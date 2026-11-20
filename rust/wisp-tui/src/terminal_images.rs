//! Terminal image rendering: capability detection, safe decoding, thumbnail layout,
//! half-block fallback, and Kitty / iTerm2 / Sixel native placement management.

use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::OpenOptions;
use std::io::{Cursor, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine as _;
use image::imageops::FilterType;
use image::{ImageEncoder as _, RgbaImage};
use ratatui::buffer::{Buffer, Cell};
use ratatui::layout::Rect;
use ratatui::style::{Color, Modifier, Style};
use sha2::{Digest, Sha256};

use crate::color_depth::ColorDepth;
use crate::composer_images::{MAX_IMAGE_BYTES, TranscriptImage, TranscriptImageSource};
use crate::transcript::{Transcript, TranscriptEntryId};
use crate::transcript_view::TranscriptRowCache;

/// Environment override forcing a specific inline image protocol (`kitty`, `iterm2`,
/// `sixel`, `halfblock`, or `auto`).
pub(crate) const OVERRIDE_VARIABLE: &str = "WISP_TUI_IMAGES";

/// Maximum thumbnail box width in terminal columns.
pub(crate) const THUMBNAIL_MAX_COLS: u16 = 36;

/// Maximum thumbnail box height in terminal rows.
pub(crate) const THUMBNAIL_MAX_ROWS: u16 = 10;

/// Maximum number of image thumbnails drawn under a single transcript entry.
pub(crate) const MAX_PREVIEW_IMAGES_PER_ENTRY: usize = 4;

/// Maximum pixel dimensions retained in decoded thumbnail buffers.
const MAX_THUMBNAIL_PX_WIDTH: u32 = 576;
const MAX_THUMBNAIL_PX_HEIGHT: u32 = 320;

/// Strict decoder safety bounds matching the backend's image ingestion limits.
const MAX_DECODE_SIDE: u32 = 8000;
const MAX_DECODE_ALLOC_BYTES: u64 = 64 * 1024 * 1024;

/// Bounds on in-memory decoded thumbnails and sticky negative lookup cache.
const MAX_CACHED_IMAGES: usize = 32;
const MAX_FAILED_KEYS: usize = 128;

/// Chunk size for Kitty Graphics Protocol base64 payloads.
const KITTY_CHUNK_BYTES: usize = 4096;

/// Delete all Kitty image placements across the active screen on shutdown or suspend.
pub(crate) const KITTY_DELETE_ALL: &[u8] = b"\x1b_Ga=d,d=a,q=2\x1b\\";

/// Graphics protocol used to render thumbnails on the live terminal.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) enum ImageProtocol {
    Kitty,
    ITerm2,
    Sixel,
    #[default]
    HalfBlock,
}

impl ImageProtocol {
    /// Detect the terminal's image protocol from environment variables, falling back to
    /// [`Self::HalfBlock`] when `NO_COLOR`, `Ansi16`, `TMUX`, or an unknown terminal is active.
    pub(crate) fn from_env(color_depth: ColorDepth, no_color: bool) -> Self {
        Self::detect(|name| std::env::var(name).ok(), color_depth, no_color)
    }

    pub(crate) fn detect(
        var: impl Fn(&str) -> Option<String>,
        color_depth: ColorDepth,
        no_color: bool,
    ) -> Self {
        if let Some(override_value) = var(OVERRIDE_VARIABLE) {
            match override_value.trim().to_ascii_lowercase().as_str() {
                "kitty" => return Self::Kitty,
                "iterm2" | "iterm" => return Self::ITerm2,
                "sixel" => return Self::Sixel,
                "halfblock" | "half-block" | "half_block" | "unicode" => return Self::HalfBlock,
                _ => {}
            }
        }

        if no_color || color_depth == ColorDepth::Ansi16 {
            return Self::HalfBlock;
        }
        if var("TMUX").is_some_and(|value| !value.trim().is_empty()) {
            return Self::HalfBlock;
        }

        let term_program = var("TERM_PROGRAM")
            .unwrap_or_default()
            .trim()
            .to_ascii_lowercase();
        let term = var("TERM").unwrap_or_default().trim().to_ascii_lowercase();

        if var("KITTY_WINDOW_ID").is_some_and(|value| !value.trim().is_empty())
            || matches!(term_program.as_str(), "ghostty" | "wezterm" | "kitty")
            || term.contains("kitty")
            || term.contains("ghostty")
        {
            return Self::Kitty;
        }

        if var("ITERM_SESSION_ID").is_some_and(|value| !value.trim().is_empty())
            || matches!(term_program.as_str(), "iterm.app" | "iterm2")
        {
            return Self::ITerm2;
        }

        if matches!(
            term_program.as_str(),
            "foot" | "mlterm" | "contour" | "blackbox" | "mintty"
        ) || term.contains("sixel")
            || term.contains("foot")
            || term.contains("mlterm")
        {
            return Self::Sixel;
        }

        Self::HalfBlock
    }
}

/// Terminal cell pixel dimensions used to keep thumbnail aspect ratios accurate.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct CellSize {
    pub width_px: u16,
    pub height_px: u16,
}

impl Default for CellSize {
    fn default() -> Self {
        Self {
            width_px: 8,
            height_px: 16,
        }
    }
}

impl CellSize {
    /// Query pixel dimensions per terminal cell via `TIOCGWINSZ`, falling back to `8×16`.
    pub(crate) fn query_stdout() -> Self {
        let Ok(window) = crossterm::terminal::window_size() else {
            return Self::default();
        };
        Self::from_window_size(window.columns, window.rows, window.width, window.height)
    }

    pub(crate) fn from_window_size(columns: u16, rows: u16, width_px: u16, height_px: u16) -> Self {
        if columns == 0 || rows == 0 || width_px == 0 || height_px == 0 {
            return Self::default();
        }
        Self {
            width_px: (width_px / columns).clamp(4, 64),
            height_px: (height_px / rows).clamp(6, 128),
        }
    }
}

/// Compute aspect-ratio-preserving `(cols, rows)` fitting an image inside `max_cols × max_rows`.
pub(crate) fn thumbnail_cells(
    px_w: u32,
    px_h: u32,
    cell: CellSize,
    max_cols: u16,
    max_rows: u16,
) -> (u16, u16) {
    if px_w == 0 || px_h == 0 || max_cols == 0 || max_rows == 0 {
        return (0, 0);
    }
    let cell_w = f64::from(cell.width_px.max(1));
    let cell_h = f64::from(cell.height_px.max(1));
    let box_w = f64::from(max_cols) * cell_w;
    let box_h = f64::from(max_rows) * cell_h;
    let scale = (box_w / f64::from(px_w))
        .min(box_h / f64::from(px_h))
        .min(1.0);
    let target_w = f64::from(px_w) * scale;
    let target_h = f64::from(px_h) * scale;
    let cols = (target_w / cell_w).round().clamp(1.0, f64::from(max_cols)) as u16;
    let rows = (target_h / cell_h).round().clamp(1.0, f64::from(max_rows)) as u16;
    (cols, rows)
}

/// Content-addressed key for an image thumbnail (`"<sha256>:<byte_size>"`).
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct ImageCacheKey(pub Arc<str>);

/// Layout-facing metadata for a decoded image thumbnail without owning pixel bytes.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ImagePreviewMeta {
    pub key: ImageCacheKey,
    pub pixel_width: u32,
    pub pixel_height: u32,
}

/// Decoded RGBA thumbnail buffered inside [`ImageCache`].
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct DecodedThumbnail {
    pub pixel_width: u32,
    pub pixel_height: u32,
    pub rgba_width: u32,
    pub rgba_height: u32,
    pub rgba: Arc<[u8]>,
}

/// Read an image from `images_dir/<sha256>` without following symlinks and verify its
/// exact byte length and SHA-256 digest.
pub(crate) fn load_stored_bytes(
    images_dir: &Path,
    sha256: &str,
    expected_size: usize,
) -> Option<Vec<u8>> {
    if sha256.len() != 64
        || !sha256
            .bytes()
            .all(|byte| matches!(byte, b'0'..=b'9' | b'a'..=b'f'))
        || expected_size == 0
        || expected_size > MAX_IMAGE_BYTES
    {
        return None;
    }

    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt as _;
        // Open non-blocking so a writerless FIFO cannot freeze LiveUi::draw before is_file rejects it.
        options.custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK);
    }
    let file = options.open(images_dir.join(sha256)).ok()?;
    let metadata = file.metadata().ok()?;
    if !metadata.is_file() || metadata.len() != u64::try_from(expected_size).ok()? {
        return None;
    }

    let mut bytes = Vec::with_capacity(expected_size);
    file.take(u64::try_from(expected_size).ok()?.saturating_add(1))
        .read_to_end(&mut bytes)
        .ok()?;
    if bytes.len() != expected_size {
        return None;
    }

    let digest = Sha256::digest(&bytes);
    let actual_hex = digest_hex(&digest);
    (actual_hex == sha256).then_some(bytes)
}

fn digest_hex(digest: &[u8]) -> String {
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        use std::fmt::Write as _;
        let _ = write!(hex, "{byte:02x}");
    }
    hex
}

/// Safely decode PNG, JPEG, GIF, or WebP bytes into a bounded RGBA thumbnail.
pub(crate) fn decode_thumbnail(bytes: &[u8]) -> Option<DecodedThumbnail> {
    let mut reader = image::ImageReader::new(Cursor::new(bytes))
        .with_guessed_format()
        .ok()?;
    if !matches!(
        reader.format(),
        Some(
            image::ImageFormat::Png
                | image::ImageFormat::Jpeg
                | image::ImageFormat::Gif
                | image::ImageFormat::WebP
        )
    ) {
        return None;
    }
    let mut limits = image::Limits::default();
    limits.max_image_width = Some(MAX_DECODE_SIDE);
    limits.max_image_height = Some(MAX_DECODE_SIDE);
    limits.max_alloc = Some(MAX_DECODE_ALLOC_BYTES);
    reader.limits(limits);

    let decoded = reader.decode().ok()?;
    let pixel_width = decoded.width();
    let pixel_height = decoded.height();
    if pixel_width == 0 || pixel_height == 0 {
        return None;
    }

    let scaled = if pixel_width > MAX_THUMBNAIL_PX_WIDTH || pixel_height > MAX_THUMBNAIL_PX_HEIGHT {
        decoded.resize(
            MAX_THUMBNAIL_PX_WIDTH,
            MAX_THUMBNAIL_PX_HEIGHT,
            FilterType::Triangle,
        )
    } else {
        decoded
    };
    let rgba = scaled.into_rgba8();
    let rgba_width = rgba.width();
    let rgba_height = rgba.height();
    if rgba_width == 0 || rgba_height == 0 {
        return None;
    }

    Some(DecodedThumbnail {
        pixel_width,
        pixel_height,
        rgba_width,
        rgba_height,
        rgba: Arc::from(rgba.into_raw()),
    })
}

/// Bounded LRU cache of decoded image thumbnails and sticky failed keys for the session.
#[derive(Debug, Default)]
pub(crate) struct ImageCache {
    entries: HashMap<ImageCacheKey, Arc<DecodedThumbnail>>,
    order: VecDeque<ImageCacheKey>,
    failed: HashSet<ImageCacheKey>,
    failed_order: VecDeque<ImageCacheKey>,
    last_images_dir: Option<PathBuf>,
}

impl ImageCache {
    /// Look up a cached decoded thumbnail without mutating LRU order.
    pub(crate) fn get(&self, key: &ImageCacheKey) -> Option<&Arc<DecodedThumbnail>> {
        self.entries.get(key)
    }

    /// Decode visible and nearby transcript images into memory and sync layout metadata
    /// into `row_cache`.
    pub(crate) fn prepare_transcript(
        &mut self,
        transcript: &Transcript,
        focus_entry: Option<TranscriptEntryId>,
        session_images_dir: Option<&Path>,
        cell_size: CellSize,
        row_cache: &mut TranscriptRowCache,
    ) {
        if self.last_images_dir.as_deref() != session_images_dir {
            self.last_images_dir = session_images_dir.map(Path::to_path_buf);
            self.failed.clear();
            self.failed_order.clear();
        }

        let entries = transcript.entries();
        if entries.is_empty() {
            row_cache.set_image_previews(HashMap::new(), cell_size);
            return;
        }

        let focus_index = focus_entry
            .and_then(|id| transcript.entry_index(id))
            .unwrap_or(entries.len() - 1)
            .min(entries.len() - 1);

        let mut previews: HashMap<TranscriptEntryId, Vec<ImagePreviewMeta>> = HashMap::new();
        let mut active_keys: HashSet<ImageCacheKey> = HashSet::new();

        for index in outward_indices(entries.len(), focus_index) {
            if active_keys.len() >= MAX_CACHED_IMAGES {
                break;
            }
            let entry = &entries[index];
            let entry_images = entry.preview_images();
            if entry_images.is_empty() {
                continue;
            }
            let mut entry_metas = Vec::new();
            for image in entry_images.iter().take(MAX_PREVIEW_IMAGES_PER_ENTRY) {
                if active_keys.len() >= MAX_CACHED_IMAGES
                    && !self.entries.contains_key(&ImageCacheKey(image.cache_key()))
                {
                    break;
                }
                let Some(thumb) = self.ensure_loaded(image, session_images_dir) else {
                    continue;
                };
                let key = ImageCacheKey(image.cache_key());
                active_keys.insert(key.clone());
                entry_metas.push(ImagePreviewMeta {
                    key,
                    pixel_width: thumb.pixel_width,
                    pixel_height: thumb.pixel_height,
                });
            }
            if !entry_metas.is_empty() {
                previews.insert(entry.id, entry_metas);
            }
        }

        row_cache.set_image_previews(previews, cell_size);
    }

    fn ensure_loaded(
        &mut self,
        image: &TranscriptImage,
        session_images_dir: Option<&Path>,
    ) -> Option<Arc<DecodedThumbnail>> {
        let key = ImageCacheKey(image.cache_key());
        if let Some(existing) = self.entries.get(&key).cloned() {
            self.touch(&key);
            return Some(existing);
        }
        if self.failed.contains(&key) {
            return None;
        }

        let decoded = match &image.source {
            TranscriptImageSource::Inline { bytes, .. } => decode_thumbnail(bytes),
            TranscriptImageSource::Stored { sha256, byte_size } => {
                let images_dir = session_images_dir?;
                load_stored_bytes(images_dir, sha256, *byte_size)
                    .and_then(|bytes| decode_thumbnail(&bytes))
            }
        };

        if let Some(thumb) = decoded {
            let thumb = Arc::new(thumb);
            self.insert(key, Arc::clone(&thumb));
            Some(thumb)
        } else {
            self.record_failed(key);
            None
        }
    }

    fn touch(&mut self, key: &ImageCacheKey) {
        self.order.retain(|existing| existing != key);
        self.order.push_back(key.clone());
    }

    fn insert(&mut self, key: ImageCacheKey, thumb: Arc<DecodedThumbnail>) {
        self.entries.insert(key.clone(), thumb);
        self.touch(&key);
        while self.entries.len() > MAX_CACHED_IMAGES {
            let Some(evicted) = self.order.pop_front() else {
                break;
            };
            self.entries.remove(&evicted);
        }
    }

    fn record_failed(&mut self, key: ImageCacheKey) {
        if self.failed.insert(key.clone()) {
            self.failed_order.push_back(key);
            while self.failed.len() > MAX_FAILED_KEYS {
                let Some(evicted) = self.failed_order.pop_front() else {
                    break;
                };
                self.failed.remove(&evicted);
            }
        }
    }
}

fn outward_indices(len: usize, center: usize) -> impl Iterator<Item = usize> {
    let mut indices = Vec::with_capacity(len);
    if len == 0 {
        return indices.into_iter();
    }
    let center = center.min(len - 1);
    indices.push(center);
    for distance in 1..len {
        if let Some(earlier) = center.checked_sub(distance) {
            indices.push(earlier);
        }
        let later = center.saturating_add(distance);
        if later < len {
            indices.push(later);
        }
    }
    indices.into_iter()
}

/// On-screen span recorded while rendering transcript image rows, checked for overlay
/// occlusion in `finish` before painting half-blocks or syncing native placements.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PaintedImage {
    pub entry_id: TranscriptEntryId,
    pub image_index: u8,
    pub key: ImageCacheKey,
    pub rect: Rect,
    pub total_rows: u16,
    pub first_image_row: u16,
    pub bg: Color,
    pub snapshot: Vec<Cell>,
}

/// Slice the vertical pixel band `[first_row .. first_row + visible_rows)` out of `total_rows`.
pub(crate) fn crop_visible_rows(
    thumb: &DecodedThumbnail,
    first_row: u16,
    visible_rows: u16,
    total_rows: u16,
) -> Option<(u32, u32, &[u8])> {
    if total_rows == 0 || visible_rows == 0 || thumb.rgba_width == 0 || thumb.rgba_height == 0 {
        return None;
    }
    let first = u32::from(first_row.min(total_rows));
    let end = u32::from(first_row.saturating_add(visible_rows).min(total_rows));
    if end <= first {
        return None;
    }
    let total = u32::from(total_rows);
    let mut y0 = (first * thumb.rgba_height) / total;
    let mut y1 = (end * thumb.rgba_height).div_ceil(total);
    if y0 >= thumb.rgba_height {
        y0 = thumb.rgba_height - 1;
    }
    y1 = y1.clamp(y0 + 1, thumb.rgba_height);
    let row_bytes = usize::try_from(thumb.rgba_width).ok()?.checked_mul(4)?;
    let start = usize::try_from(y0).ok()?.checked_mul(row_bytes)?;
    let stop = usize::try_from(y1).ok()?.checked_mul(row_bytes)?;
    let slice = thumb.rgba.get(start..stop)?;
    Some((thumb.rgba_width, y1 - y0, slice))
}

fn bg_rgb(color: Color) -> (u8, u8, u8) {
    match color {
        Color::Rgb(r, g, b) => (r, g, b),
        Color::Black => (0, 0, 0),
        Color::Red => (170, 0, 0),
        Color::Green => (0, 170, 0),
        Color::Yellow => (170, 85, 0),
        Color::Blue => (0, 0, 170),
        Color::Magenta => (170, 0, 170),
        Color::Cyan => (0, 170, 170),
        Color::Gray => (170, 170, 170),
        Color::DarkGray => (85, 85, 85),
        Color::LightRed => (255, 85, 85),
        Color::LightGreen => (85, 255, 85),
        Color::LightYellow => (255, 255, 85),
        Color::LightBlue => (85, 85, 255),
        Color::LightMagenta => (255, 85, 255),
        Color::LightCyan => (85, 255, 255),
        Color::White => (255, 255, 255),
        Color::Reset | Color::Indexed(_) => (0, 0, 0),
    }
}

fn composite_pixel(pixel: &[u8], bg: (u8, u8, u8), monochrome: bool) -> (u8, u8, u8) {
    let alpha = u16::from(pixel[3]);
    let inv = 255 - alpha;
    let r = ((u16::from(pixel[0]) * alpha + u16::from(bg.0) * inv + 127) / 255) as u8;
    let g = ((u16::from(pixel[1]) * alpha + u16::from(bg.1) * inv + 127) / 255) as u8;
    let b = ((u16::from(pixel[2]) * alpha + u16::from(bg.2) * inv + 127) / 255) as u8;
    if monochrome {
        let luma =
            ((2126 * u32::from(r) + 7152 * u32::from(g) + 722 * u32::from(b) + 5000) / 10000) as u8;
        (luma, luma, luma)
    } else {
        (r, g, b)
    }
}

/// Paint `painted.rect` in `buffer` using Unicode upper half-blocks (`▀`), skipping
/// any cell marked `false` in `uncovered` so active overlays remain intact.
pub(crate) fn paint_half_blocks(
    buffer: &mut Buffer,
    thumb: &DecodedThumbnail,
    painted: &PaintedImage,
    monochrome: bool,
    uncovered: Option<&[bool]>,
) {
    let clipped = painted.rect.intersection(buffer.area);
    if clipped.is_empty() {
        return;
    }
    let Some((crop_w, crop_h, crop_rgba)) = crop_visible_rows(
        thumb,
        painted.first_image_row,
        painted.rect.height,
        painted.total_rows,
    ) else {
        return;
    };
    let Some(crop_img) = RgbaImage::from_raw(crop_w, crop_h, crop_rgba.to_vec()) else {
        return;
    };
    let resized = image::imageops::resize(
        &crop_img,
        u32::from(painted.rect.width),
        u32::from(painted.rect.height) * 2,
        FilterType::Triangle,
    );
    let raw = resized.as_raw();
    let stride = usize::from(painted.rect.width) * 4;
    let bg = bg_rgb(painted.bg);

    for dy in 0..painted.rect.height {
        let y = painted.rect.y + dy;
        if y < clipped.top() || y >= clipped.bottom() {
            continue;
        }
        let top_row = usize::from(dy) * 2 * stride;
        let bot_row = top_row + stride;
        for dx in 0..painted.rect.width {
            let x = painted.rect.x + dx;
            if x < clipped.left() || x >= clipped.right() {
                continue;
            }
            let cell_idx = usize::from(dy) * usize::from(painted.rect.width) + usize::from(dx);
            if uncovered.is_some_and(|mask| !mask.get(cell_idx).copied().unwrap_or(false)) {
                continue;
            }
            let px_offset = usize::from(dx) * 4;
            let top = composite_pixel(
                &raw[top_row + px_offset..top_row + px_offset + 4],
                bg,
                monochrome,
            );
            let bot = composite_pixel(
                &raw[bot_row + px_offset..bot_row + px_offset + 4],
                bg,
                monochrome,
            );
            let cell = &mut buffer[(x, y)];
            cell.set_symbol("▀");
            cell.set_style(
                Style::default()
                    .fg(Color::Rgb(top.0, top.1, top.2))
                    .bg(Color::Rgb(bot.0, bot.1, bot.2)),
            );
            cell.modifier = Modifier::empty();
        }
    }
}

/// Emit Kitty Graphics Protocol chunked RGBA transmission for `rect` at ID `id`.
pub(crate) fn encode_kitty(
    out: &mut Vec<u8>,
    id: u32,
    rect: Rect,
    rgba: &[u8],
    width: u32,
    height: u32,
) {
    if rect.is_empty() || rgba.is_empty() || width == 0 || height == 0 {
        return;
    }
    let _ = write!(out, "\x1b[{};{}H", rect.y + 1, rect.x + 1);
    let encoded = base64::engine::general_purpose::STANDARD.encode(rgba);
    let chunks: Vec<&[u8]> = encoded.as_bytes().chunks(KITTY_CHUNK_BYTES).collect();
    for (index, chunk) in chunks.iter().enumerate() {
        let more = usize::from(index + 1 < chunks.len());
        if index == 0 {
            let _ = write!(
                out,
                "\x1b_Ga=T,f=32,s={width},v={height},c={},r={},i={id},p=1,C=1,q=2,m={more};",
                rect.width, rect.height
            );
        } else {
            let _ = write!(out, "\x1b_Gm={more};");
        }
        out.extend_from_slice(chunk);
        out.extend_from_slice(b"\x1b\\");
    }
}

/// Emit Kitty Graphics Protocol deletion command for image ID `id`.
pub(crate) fn encode_kitty_delete(out: &mut Vec<u8>, id: u32) {
    let _ = write!(out, "\x1b_Ga=d,d=i,i={id},q=2\x1b\\");
}

/// Emit an iTerm2 inline image (`OSC 1337`) composited onto `bg` inside `rect`.
pub(crate) fn encode_iterm2(
    out: &mut Vec<u8>,
    rect: Rect,
    rgba: &[u8],
    width: u32,
    height: u32,
    bg: Color,
) {
    if rect.is_empty() || rgba.is_empty() || width == 0 || height == 0 {
        return;
    }
    let bg = bg_rgb(bg);
    let mut rgb = Vec::with_capacity((rgba.len() / 4) * 3);
    for pixel in rgba.chunks_exact(4) {
        let (r, g, b) = composite_pixel(pixel, bg, false);
        rgb.extend_from_slice(&[r, g, b]);
    }
    let mut png = Vec::new();
    let encoder = image::codecs::png::PngEncoder::new(&mut png);
    if encoder
        .write_image(&rgb, width, height, image::ExtendedColorType::Rgb8)
        .is_err()
    {
        return;
    }
    let encoded = base64::engine::general_purpose::STANDARD.encode(&png);
    let _ = write!(
        out,
        "\x1b[{};{}H\x1b]1337;File=inline=1;size={};width={};height={};preserveAspectRatio=0:{}\x07",
        rect.y + 1,
        rect.x + 1,
        png.len(),
        rect.width,
        rect.height,
        encoded
    );
}

/// Emit a Sixel graphics sequence (`DCS P0;1q ... ST`) sized to `rect × cell_size`.
pub(crate) fn encode_sixel(
    out: &mut Vec<u8>,
    rect: Rect,
    rgba: &[u8],
    width: u32,
    height: u32,
    cell: CellSize,
    bg: Color,
) {
    if rect.is_empty() || rgba.is_empty() || width == 0 || height == 0 {
        return;
    }
    let Some(crop_img) = RgbaImage::from_raw(width, height, rgba.to_vec()) else {
        return;
    };
    let target_w = (u32::from(rect.width) * u32::from(cell.width_px.max(1))).clamp(1, 1024);
    let target_h = (u32::from(rect.height) * u32::from(cell.height_px.max(1))).clamp(1, 1024);
    let resized = image::imageops::resize(&crop_img, target_w, target_h, FilterType::Triangle);
    let raw = resized.as_raw();
    let bg = bg_rgb(bg);

    // Uniform 6×6×6 RGB cube (216 registers) with Bayer 2×2 ordered dither.
    const BAYER2: [[i16; 2]; 2] = [[-16, 16], [24, -8]];
    let mut indices = Vec::with_capacity(usize::try_from(target_w * target_h).unwrap_or(0));
    let mut used = [false; 216];
    let quant_step = |channel: u8, bias: i16| -> u8 {
        let adjusted = (i16::from(channel) + bias).clamp(0, 255) as u16;
        ((adjusted * 5 + 127) / 255) as u8
    };
    for y in 0..target_h {
        for x in 0..target_w {
            let offset = (usize::try_from(y * target_w + x).unwrap_or(0)) * 4;
            let (r, g, b) = composite_pixel(&raw[offset..offset + 4], bg, false);
            let bias =
                BAYER2[usize::try_from(y & 1).unwrap_or(0)][usize::try_from(x & 1).unwrap_or(0)];
            let rs = quant_step(r, bias);
            let gs = quant_step(g, bias);
            let bs = quant_step(b, bias);
            let reg = usize::from(rs) * 36 + usize::from(gs) * 6 + usize::from(bs);
            used[reg] = true;
            indices.push(reg as u8);
        }
    }

    let _ = write!(
        out,
        "\x1b[{};{}H\x1bP0;1q\"1;1;{target_w};{target_h}",
        rect.y + 1,
        rect.x + 1
    );
    for (reg, &is_used) in used.iter().enumerate() {
        if !is_used {
            continue;
        }
        let r_pct = (reg / 36) * 20;
        let g_pct = ((reg / 6) % 6) * 20;
        let b_pct = (reg % 6) * 20;
        let _ = write!(out, "#{reg};2;{r_pct};{g_pct};{b_pct}");
    }

    let w_usize = usize::try_from(target_w).unwrap_or(0);
    let h_usize = usize::try_from(target_h).unwrap_or(0);
    let band_count = h_usize.div_ceil(6);
    let mut band_present = [false; 216];
    let mut band_regs = Vec::new();
    let mut masks = vec![0_u8; w_usize];

    for band in 0..band_count {
        let y_start = band * 6;
        let y_end = (y_start + 6).min(h_usize);
        band_present.fill(false);
        band_regs.clear();
        for y in y_start..y_end {
            for &reg in &indices[y * w_usize..(y + 1) * w_usize] {
                let idx = usize::from(reg);
                if !band_present[idx] {
                    band_present[idx] = true;
                    band_regs.push(reg);
                }
            }
        }
        for (color_pos, &reg) in band_regs.iter().enumerate() {
            if color_pos > 0 {
                out.push(b'$');
            }
            let _ = write!(out, "#{reg}");
            let mut last_nonzero = 0_usize;
            masks.fill(0);
            for bit in 0..(y_end - y_start) {
                let row = &indices[(y_start + bit) * w_usize..(y_start + bit + 1) * w_usize];
                for (x, &pixel_reg) in row.iter().enumerate() {
                    if pixel_reg == reg {
                        masks[x] |= 1_u8 << bit;
                        last_nonzero = last_nonzero.max(x + 1);
                    }
                }
            }
            encode_sixel_rle(out, &masks[..last_nonzero]);
        }
        if band + 1 < band_count {
            out.push(b'-');
        }
    }
    out.extend_from_slice(b"\x1b\\");
}

fn encode_sixel_rle(out: &mut Vec<u8>, masks: &[u8]) {
    let mut idx = 0;
    while idx < masks.len() {
        let mask = masks[idx];
        let mut run = 1_usize;
        while idx + run < masks.len() && masks[idx + run] == mask {
            run += 1;
        }
        let ch = 0x3F + mask;
        if run >= 4 {
            let _ = write!(out, "!{run}{}", char::from(ch));
        } else {
            for _ in 0..run {
                out.push(ch);
            }
        }
        idx += run;
    }
}

/// Stable identifier of one placement slot inside a transcript entry.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub(crate) struct PlacementKey {
    pub entry_id: TranscriptEntryId,
    pub image_index: u8,
}

/// Desired native protocol placement for the current frame.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct NativePlacement {
    pub placement: PlacementKey,
    pub image_key: ImageCacheKey,
    pub rect: Rect,
    pub total_rows: u16,
    pub first_image_row: u16,
    pub bg: Color,
}

#[derive(Clone, Debug, Eq, PartialEq)]
struct ActivePlacement {
    kitty_id: u32,
    spec: NativePlacement,
    cell_size: CellSize,
}

/// Diff-based native graphics placement tracker across frames.
#[derive(Debug, Default)]
pub(crate) struct ImagePlacementState {
    active: HashMap<PlacementKey, ActivePlacement>,
    next_kitty_id: u32,
    force_reemit: bool,
}

impl ImagePlacementState {
    /// Whether any Kitty placement is currently active on the terminal.
    pub(crate) fn has_active_kitty(&self) -> bool {
        self.active.values().any(|entry| entry.kitty_id != 0)
    }

    /// Force every visible placement to be deleted and re-emitted on the next sync
    /// (for example after a terminal resize or frame invalidation).
    pub(crate) fn request_reemit(&mut self) {
        self.force_reemit = true;
    }

    /// Forget all active placements without deleting them (after `$EDITOR` suspend/resume).
    pub(crate) fn invalidate(&mut self) {
        self.active.clear();
        self.force_reemit = false;
    }

    fn alloc_kitty_id(&mut self) -> u32 {
        loop {
            self.next_kitty_id = self.next_kitty_id.wrapping_add(1);
            if self.next_kitty_id != 0 {
                return self.next_kitty_id;
            }
        }
    }

    /// Reconcile terminal native placements against `desired`, writing minimal escape
    /// updates (or deletions when scrolled off / occluded / switched to half-blocks) into `out`.
    pub(crate) fn sync(
        &mut self,
        protocol: ImageProtocol,
        desired: &[NativePlacement],
        cache: &ImageCache,
        cell_size: CellSize,
        out: &mut Vec<u8>,
    ) {
        let force_reemit = std::mem::take(&mut self.force_reemit);
        if protocol == ImageProtocol::HalfBlock {
            for existing in self.active.values() {
                if existing.kitty_id != 0 {
                    encode_kitty_delete(out, existing.kitty_id);
                }
            }
            self.active.clear();
            return;
        }

        let desired_map: HashMap<&PlacementKey, &NativePlacement> =
            desired.iter().map(|item| (&item.placement, item)).collect();

        // Remove placements that disappeared, changed geometry/image/cell_size, or need re-emit.
        let mut stale_keys = Vec::new();
        for (key, existing) in &self.active {
            let matches = !force_reemit
                && desired_map
                    .get(key)
                    .is_some_and(|want| &existing.spec == *want && existing.cell_size == cell_size);
            if !matches {
                if protocol == ImageProtocol::Kitty && existing.kitty_id != 0 {
                    encode_kitty_delete(out, existing.kitty_id);
                }
                stale_keys.push(key.clone());
            }
        }
        for key in stale_keys {
            self.active.remove(&key);
        }

        // Emit any new or updated placements in stable desired order.
        for item in desired {
            if self.active.contains_key(&item.placement) {
                continue;
            }
            let Some(thumb) = cache.get(&item.image_key) else {
                continue;
            };
            let Some((crop_w, crop_h, crop_rgba)) = crop_visible_rows(
                thumb,
                item.first_image_row,
                item.rect.height,
                item.total_rows,
            ) else {
                continue;
            };
            let kitty_id = match protocol {
                ImageProtocol::Kitty => {
                    let id = self.alloc_kitty_id();
                    encode_kitty(out, id, item.rect, crop_rgba, crop_w, crop_h);
                    id
                }
                ImageProtocol::ITerm2 => {
                    encode_iterm2(out, item.rect, crop_rgba, crop_w, crop_h, item.bg);
                    0
                }
                ImageProtocol::Sixel => {
                    encode_sixel(
                        out, item.rect, crop_rgba, crop_w, crop_h, cell_size, item.bg,
                    );
                    0
                }
                ImageProtocol::HalfBlock => unreachable!("handled above"),
            };
            self.active.insert(
                item.placement.clone(),
                ActivePlacement {
                    kitty_id,
                    spec: item.clone(),
                    cell_size,
                },
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    struct TempDir(PathBuf);

    impl TempDir {
        fn new(tag: &str) -> Self {
            static SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
            let id = SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let path =
                std::env::temp_dir().join(format!("wisp-tui-{tag}-{}-{id}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            std::fs::create_dir_all(&path).unwrap();
            Self(path)
        }

        fn path(&self) -> &Path {
            &self.0
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn env_map<'a>(pairs: &[(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        let map: BTreeMap<&str, &str> = pairs.iter().copied().collect();
        move |key| map.get(key).map(|value| (*value).to_owned())
    }

    fn sample_png_bytes(width: u32, height: u32) -> Vec<u8> {
        let mut img = RgbaImage::new(width, height);
        for (x, y, pixel) in img.enumerate_pixels_mut() {
            let r = ((x * 255) / width.max(1)) as u8;
            let g = ((y * 255) / height.max(1)) as u8;
            *pixel = image::Rgba([r, g, 128, 255]);
        }
        let mut encoded = Vec::new();
        let encoder = image::codecs::png::PngEncoder::new(&mut encoded);
        encoder
            .write_image(img.as_raw(), width, height, image::ExtendedColorType::Rgba8)
            .unwrap();
        encoded
    }

    #[test]
    fn detects_protocols_and_overrides() {
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("WISP_TUI_IMAGES", "sixel"), ("KITTY_WINDOW_ID", "1")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::Sixel
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[
                    ("WISP_TUI_IMAGES", "half-block"),
                    ("TERM_PROGRAM", "ghostty")
                ]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::HalfBlock
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("TERM_PROGRAM", "ghostty")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::Kitty
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("TERM_PROGRAM", "WezTerm")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::Kitty
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("TERM_PROGRAM", "iTerm.app")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::ITerm2
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("TERM_PROGRAM", "foot")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::Sixel
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("KITTY_WINDOW_ID", "1"), ("TMUX", "/tmp/tmux-1000,1,0")]),
                ColorDepth::TrueColor,
                false,
            ),
            ImageProtocol::HalfBlock
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("KITTY_WINDOW_ID", "1")]),
                ColorDepth::Ansi16,
                false,
            ),
            ImageProtocol::HalfBlock
        );
        assert_eq!(
            ImageProtocol::detect(
                env_map(&[("KITTY_WINDOW_ID", "1")]),
                ColorDepth::TrueColor,
                true,
            ),
            ImageProtocol::HalfBlock
        );
    }

    #[test]
    fn thumbnail_cells_preserves_aspect_ratio_and_bounds() {
        let cell = CellSize::default(); // 8x16, max box = 288x160
        assert_eq!(thumbnail_cells(800, 400, cell, 36, 10), (36, 9));
        assert_eq!(thumbnail_cells(400, 800, cell, 36, 10), (10, 10));
        assert_eq!(thumbnail_cells(1000, 1000, cell, 36, 10), (20, 10));
        assert_eq!(thumbnail_cells(16, 16, cell, 36, 10), (2, 1));
        assert_eq!(thumbnail_cells(0, 100, cell, 36, 10), (0, 0));
        assert_eq!(thumbnail_cells(100, 100, cell, 0, 10), (0, 0));
    }

    #[test]
    fn load_stored_bytes_validates_digest_size_and_symlinks() {
        let dir = TempDir::new("stored-bytes");
        let bytes = sample_png_bytes(16, 16);
        let hex = digest_hex(&Sha256::digest(&bytes));
        std::fs::write(dir.path().join(&hex), &bytes).unwrap();

        assert_eq!(
            load_stored_bytes(dir.path(), &hex, bytes.len()).as_deref(),
            Some(bytes.as_slice())
        );
        assert_eq!(load_stored_bytes(dir.path(), &hex, bytes.len() + 1), None);
        assert_eq!(load_stored_bytes(dir.path(), "not-hex", bytes.len()), None);

        // Corrupt the stored file while preserving size.
        let mut tampered = bytes.clone();
        tampered[0] ^= 0xff;
        std::fs::write(dir.path().join(&hex), &tampered).unwrap();
        assert_eq!(load_stored_bytes(dir.path(), &hex, tampered.len()), None);

        #[cfg(unix)]
        {
            let target = dir.path().join("target.png");
            std::fs::write(&target, &bytes).unwrap();
            let link_hex = "a".repeat(64);
            std::os::unix::fs::symlink(&target, dir.path().join(&link_hex)).unwrap();
            assert_eq!(load_stored_bytes(dir.path(), &link_hex, bytes.len()), None);
        }
    }

    #[cfg(unix)]
    #[test]
    fn load_stored_bytes_rejects_fifo_without_blocking() {
        let dir = TempDir::new("stored-bytes-fifo");
        let fifo_hex = "b".repeat(64);
        let fifo_path = dir.path().join(&fifo_hex);
        let status = std::process::Command::new("mkfifo")
            .arg(&fifo_path)
            .status()
            .expect("mkfifo command should run on unix");
        assert!(status.success(), "mkfifo failed: {status}");

        let (tx, rx) = std::sync::mpsc::channel();
        let dir_path = dir.path().to_path_buf();
        std::thread::spawn(move || {
            let _ = tx.send(load_stored_bytes(&dir_path, &fifo_hex, 128));
        });

        assert_eq!(rx.recv_timeout(std::time::Duration::from_secs(2)), Ok(None));
    }

    #[test]
    fn decode_thumbnail_supports_multiple_formats_and_downscales() {
        let png = sample_png_bytes(1200, 600);
        let thumb = decode_thumbnail(&png).unwrap();
        assert_eq!((thumb.pixel_width, thumb.pixel_height), (1200, 600));
        assert_eq!((thumb.rgba_width, thumb.rgba_height), (576, 288));

        // JPEG encoding + decode.
        let img = image::DynamicImage::ImageRgba8(RgbaImage::from_pixel(
            32,
            24,
            image::Rgba([10, 200, 30, 255]),
        ));
        let mut jpg = Vec::new();
        img.write_to(&mut Cursor::new(&mut jpg), image::ImageFormat::Jpeg)
            .unwrap();
        let jpg_thumb = decode_thumbnail(&jpg).unwrap();
        assert_eq!((jpg_thumb.pixel_width, jpg_thumb.pixel_height), (32, 24));

        // GIF encoding + decode.
        let mut gif = Vec::new();
        img.write_to(&mut Cursor::new(&mut gif), image::ImageFormat::Gif)
            .unwrap();
        let gif_thumb = decode_thumbnail(&gif).unwrap();
        assert_eq!((gif_thumb.pixel_width, gif_thumb.pixel_height), (32, 24));

        // WebP encoding + decode.
        let mut webp = Vec::new();
        img.write_to(&mut Cursor::new(&mut webp), image::ImageFormat::WebP)
            .unwrap();
        let webp_thumb = decode_thumbnail(&webp).unwrap();
        assert_eq!((webp_thumb.pixel_width, webp_thumb.pixel_height), (32, 24));

        // Corrupt input fails safely.
        assert_eq!(decode_thumbnail(b"not an image"), None);
    }

    #[test]
    fn half_blocks_blend_alpha_respect_monochrome_and_uncovered_mask() {
        let thumb = DecodedThumbnail {
            pixel_width: 2,
            pixel_height: 2,
            rgba_width: 2,
            rgba_height: 2,
            rgba: Arc::from([
                200, 100, 50, 255, // top-left opaque
                0, 200, 0, 128, // top-right 50% green
                20, 40, 200, 255, // bottom-left opaque
                0, 0, 0, 0, // bottom-right transparent
            ]),
        };
        let mut buffer = Buffer::empty(Rect::new(0, 0, 2, 1));
        buffer[(1, 0)].set_symbol("X");
        let painted = PaintedImage {
            entry_id: TranscriptEntryId::from_raw(1),
            image_index: 0,
            key: ImageCacheKey(Arc::from("k")),
            rect: Rect::new(0, 0, 2, 1),
            total_rows: 1,
            first_image_row: 0,
            bg: Color::Rgb(10, 20, 30),
            snapshot: Vec::new(),
        };
        // Leave second column covered (false).
        paint_half_blocks(&mut buffer, &thumb, &painted, false, Some(&[true, false]));
        assert_eq!(buffer[(0, 0)].symbol(), "▀");
        assert_ne!(buffer[(0, 0)].fg, buffer[(0, 0)].bg);
        assert_eq!(buffer[(1, 0)].symbol(), "X");

        // Monochrome sets R == G == B.
        paint_half_blocks(&mut buffer, &thumb, &painted, true, None);
        assert_eq!(buffer[(1, 0)].symbol(), "▀");
        if let Color::Rgb(r, g, b) = buffer[(0, 0)].fg {
            assert!(r == g && g == b);
        } else {
            panic!("expected Rgb monochrome color");
        }
        // Transparent pixel over bg(10, 20, 30) gives monochrome luma of bg.
        if let Color::Rgb(r, g, b) = buffer[(1, 0)].bg {
            assert!(r == g && g == b && r > 0);
        } else {
            panic!("expected Rgb monochrome bg");
        }
    }

    #[test]
    fn native_encoders_produce_valid_escape_frames() {
        let rgba = vec![255_u8; 4 * 4 * 4];
        let mut kitty = Vec::new();
        encode_kitty(&mut kitty, 7, Rect::new(2, 3, 4, 2), &rgba, 4, 4);
        let kitty_str = String::from_utf8(kitty).unwrap();
        assert!(
            kitty_str.starts_with("\x1b[4;3H\x1b_Ga=T,f=32,s=4,v=4,c=4,r=2,i=7,p=1,C=1,q=2,m=0;")
        );
        assert!(kitty_str.ends_with("\x1b\\"));

        let mut iterm = Vec::new();
        encode_iterm2(&mut iterm, Rect::new(0, 0, 4, 2), &rgba, 4, 4, Color::Black);
        let iterm_str = String::from_utf8(iterm).unwrap();
        assert!(iterm_str.starts_with("\x1b[1;1H\x1b]1337;File=inline=1;size="));
        assert!(iterm_str.ends_with("\x07"));

        let mut sixel = Vec::new();
        encode_sixel(
            &mut sixel,
            Rect::new(1, 1, 2, 1),
            &rgba,
            4,
            4,
            CellSize::default(),
            Color::Black,
        );
        let sixel_str = String::from_utf8(sixel).unwrap();
        assert!(sixel_str.starts_with("\x1b[2;2H\x1bP0;1q\"1;1;16;16"));
        assert!(sixel_str.ends_with("\x1b\\"));
    }

    #[test]
    fn placement_state_syncs_diffs_and_deletes_on_occlusion_and_scroll() {
        let mut cache = ImageCache::default();
        let png = sample_png_bytes(16, 32);
        let key = ImageCacheKey(Arc::from("test-key"));
        cache.insert(key.clone(), Arc::new(decode_thumbnail(&png).unwrap()));

        let mut placements = ImagePlacementState::default();
        let desired = vec![NativePlacement {
            placement: PlacementKey {
                entry_id: TranscriptEntryId::from_raw(1),
                image_index: 0,
            },
            image_key: key.clone(),
            rect: Rect::new(2, 1, 4, 2),
            total_rows: 2,
            first_image_row: 0,
            bg: Color::Black,
        }];

        let mut frame1 = Vec::new();
        placements.sync(
            ImageProtocol::Kitty,
            &desired,
            &cache,
            CellSize::default(),
            &mut frame1,
        );
        let f1 = String::from_utf8(frame1).unwrap();
        assert!(f1.contains("a=T,") && f1.contains(",q=2,"));
        assert!(placements.has_active_kitty());

        // Identical second frame emits zero bytes.
        let mut frame2 = Vec::new();
        placements.sync(
            ImageProtocol::Kitty,
            &desired,
            &cache,
            CellSize::default(),
            &mut frame2,
        );
        assert!(frame2.is_empty());

        // Scrolled/cropped frame deletes id=1 and emits id=2.
        let mut scrolled = desired.clone();
        scrolled[0].rect.y = 0;
        scrolled[0].rect.height = 1;
        scrolled[0].first_image_row = 1;
        let mut frame3 = Vec::new();
        placements.sync(
            ImageProtocol::Kitty,
            &scrolled,
            &cache,
            CellSize::default(),
            &mut frame3,
        );
        let f3 = String::from_utf8(frame3).unwrap();
        assert!(f3.contains("\x1b_Ga=d,d=i,i=1,q=2\x1b\\"));
        assert!(f3.contains("a=T,") && f3.contains(",i=2,"));

        // Occlusion / scroll off emits delete and clears active Kitty flag.
        let mut frame4 = Vec::new();
        placements.sync(
            ImageProtocol::Kitty,
            &[],
            &cache,
            CellSize::default(),
            &mut frame4,
        );
        let f4 = String::from_utf8(frame4).unwrap();
        assert_eq!(f4, "\x1b_Ga=d,d=i,i=2,q=2\x1b\\");
        assert!(!placements.has_active_kitty());

        // Re-adding emits id=3, and invalidate clears without emitting until next sync.
        let mut frame5 = Vec::new();
        placements.sync(
            ImageProtocol::Kitty,
            &desired,
            &cache,
            CellSize::default(),
            &mut frame5,
        );
        assert!(String::from_utf8(frame5).unwrap().contains(",i=3,"));
        placements.invalidate();
        assert!(!placements.has_active_kitty());
    }
}

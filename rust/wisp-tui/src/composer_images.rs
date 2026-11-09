//! Images attached to the composer draft: clipboard pastes and pasted image paths.
//!
//! The composer holds each image's bytes until the draft is sent. Limits mirror the
//! backend's so a paste is refused here, with a clear notice, rather than failing the
//! command after it is sent; the backend still checks every image itself.

use std::fmt;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine;
use image::ImageEncoder;

/// Largest image file the backend accepts (`wisp.images.MAX_IMAGE_BYTES`).
pub const MAX_IMAGE_BYTES: usize = 7_500_000;
/// Most images one message may carry (`wisp.rpc.commands.MAX_RPC_MESSAGE_IMAGES`).
pub const MAX_IMAGES: usize = 6;
/// Enough leading bytes to recognise every supported format.
const SNIFF_BYTES: usize = 16;

/// One image waiting in the composer.
#[derive(Clone, PartialEq, Eq)]
pub struct AttachedImage {
    bytes: Arc<[u8]>,
    media_type: &'static str,
    dimensions: Option<(u32, u32)>,
    file_name: Option<String>,
}

// Image bytes run to megabytes; a failing test assertion should print a summary.
impl fmt::Debug for AttachedImage {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("AttachedImage")
            .field("media_type", &self.media_type)
            .field("dimensions", &self.dimensions)
            .field("file_name", &self.file_name)
            .field("byte_size", &self.bytes.len())
            .finish()
    }
}

/// Why an image could not be attached.
#[derive(Clone, Debug, Eq, PartialEq, thiserror::Error)]
pub enum AttachError {
    #[error("a message can carry at most {MAX_IMAGES} images")]
    TooMany,
    #[error("the image is {size}; the limit is {limit}", size = megabytes(*.0), limit = megabytes(MAX_IMAGE_BYTES))]
    TooLarge(usize),
    #[error("{model} does not accept images")]
    ModelRejectsImages { model: String },
    #[error(
        "these images total {total}; one request to this model carries at most {limit}",
        total = megabytes(*.total),
        limit = megabytes(*.limit)
    )]
    OverRequestBudget { total: usize, limit: usize },
    #[error("could not encode the clipboard image as PNG")]
    EncodeFailed,
    #[error("could not read {0}")]
    ReadFailed(String),
}

/// What the current model accepts, from the backend's model selection.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ImageSupport<'a> {
    pub model: &'a str,
    pub accepts_images: bool,
    pub max_request_bytes: u64,
}

impl AttachedImage {
    /// Encode a clipboard bitmap (RGBA, 8 bits per channel) as PNG.
    pub fn from_rgba(width: usize, height: usize, rgba: &[u8]) -> Result<Self, AttachError> {
        // The encoder asserts on a buffer of the wrong size; refuse it instead.
        if width
            .checked_mul(height)
            .and_then(|pixels| pixels.checked_mul(4))
            != Some(rgba.len())
        {
            return Err(AttachError::EncodeFailed);
        }
        let width = u32::try_from(width).map_err(|_| AttachError::EncodeFailed)?;
        let height = u32::try_from(height).map_err(|_| AttachError::EncodeFailed)?;
        let mut png = Vec::new();
        let encoder = image::codecs::png::PngEncoder::new(&mut png);
        // Screenshots are opaque; dropping the alpha channel makes their PNG about a
        // quarter smaller, which keeps more of them under the per-image limit.
        let written = if rgba.chunks_exact(4).all(|pixel| pixel[3] == u8::MAX) {
            let rgb = rgba
                .chunks_exact(4)
                .flat_map(|pixel| [pixel[0], pixel[1], pixel[2]])
                .collect::<Vec<_>>();
            encoder.write_image(&rgb, width, height, image::ExtendedColorType::Rgb8)
        } else {
            encoder.write_image(rgba, width, height, image::ExtendedColorType::Rgba8)
        };
        written.map_err(|_| AttachError::EncodeFailed)?;
        Ok(Self {
            bytes: png.into(),
            media_type: "image/png",
            dimensions: Some((width, height)),
            file_name: None,
        })
    }

    /// Read an image file named by a paste, or `None` when the file is not an image.
    pub fn from_file(path: &Path) -> Result<Option<Self>, AttachError> {
        let display = path.display().to_string();
        let mut file =
            std::fs::File::open(path).map_err(|_| AttachError::ReadFailed(display.clone()))?;
        // Read only the header first, so a pasted path to a large log or binary costs
        // a few bytes rather than megabytes before it pastes as text.
        let mut bytes = Vec::new();
        file.by_ref()
            .take(SNIFF_BYTES as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| AttachError::ReadFailed(display.clone()))?;
        let Some(media_type) = sniff_media_type(&bytes) else {
            return Ok(None);
        };
        // One byte past the limit is enough to know the file is too large.
        file.take((MAX_IMAGE_BYTES + 1 - bytes.len()) as u64)
            .read_to_end(&mut bytes)
            .map_err(|_| AttachError::ReadFailed(display))?;
        if bytes.len() > MAX_IMAGE_BYTES {
            let size = std::fs::metadata(path).map_or(bytes.len(), |info| info.len() as usize);
            return Err(AttachError::TooLarge(size));
        }
        Ok(Some(Self {
            bytes: bytes.into(),
            media_type,
            dimensions: None,
            file_name: path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned()),
        }))
    }

    pub fn byte_size(&self) -> usize {
        self.bytes.len()
    }

    pub fn base64(&self) -> String {
        base64::engine::general_purpose::STANDARD.encode(&self.bytes)
    }

    /// The chip shown in the composer, such as `Image 1 · 1280×720`.
    pub fn chip_label(&self, number: usize) -> String {
        match (&self.dimensions, &self.file_name) {
            (Some((width, height)), _) => format!("Image {number} · {width}×{height}"),
            (None, Some(name)) => format!("Image {number} · {name}"),
            (None, None) => format!("Image {number} · {}", megabytes(self.bytes.len())),
        }
    }

    /// The line the transcript shows for this image when its message is sent.
    pub fn transcript_line(&self) -> String {
        let format = media_label(self.media_type);
        match (&self.dimensions, &self.file_name) {
            (Some((width, height)), _) => format!("[image {width}×{height} {format}]"),
            (None, Some(name)) => format!("[image {name}]"),
            (None, None) => format!("[image {format}]"),
        }
    }
}

/// The transcript line for an image the backend reports by reference.
pub fn reported_image_line(media_type: &str, width: u64, height: u64) -> String {
    format!("[image {width}×{height} {}]", media_label(media_type))
}

/// Transcript lines for the `images` array of a reported user message or event.
pub fn reported_image_lines(images: Option<&serde_json::Value>) -> Vec<String> {
    let Some(images) = images.and_then(serde_json::Value::as_array) else {
        return Vec::new();
    };
    images
        .iter()
        .filter_map(|image| {
            Some(reported_image_line(
                image.get("media_type")?.as_str()?,
                image.get("width")?.as_u64()?,
                image.get("height")?.as_u64()?,
            ))
        })
        .collect()
}

/// A user message's transcript text followed by one line per attached image.
pub fn with_image_lines(text: &str, lines: &[String]) -> String {
    match (text.is_empty(), lines.is_empty()) {
        (_, true) => text.to_owned(),
        (true, false) => lines.join("\n"),
        (false, false) => format!("{text}\n{}", lines.join("\n")),
    }
}

/// Images waiting to be sent with the draft, in attachment order.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ComposerImages {
    images: Vec<AttachedImage>,
}

impl ComposerImages {
    /// Attach an image after checking the backend's count, size and model limits.
    ///
    /// `support` is `None` until the backend has reported the model selection; the
    /// backend then remains the only judge of what the model accepts.
    pub fn attach(
        &mut self,
        image: AttachedImage,
        support: Option<ImageSupport<'_>>,
    ) -> Result<usize, AttachError> {
        if self.images.len() >= MAX_IMAGES {
            return Err(AttachError::TooMany);
        }
        if image.byte_size() > MAX_IMAGE_BYTES {
            return Err(AttachError::TooLarge(image.byte_size()));
        }
        if let Some(support) = support {
            check_support(self.total_bytes() + image.byte_size(), support)?;
        }
        self.images.push(image);
        Ok(self.images.len())
    }

    /// Check the images against a model chosen after they were attached.
    pub fn check(&self, support: Option<ImageSupport<'_>>) -> Result<(), AttachError> {
        match support {
            Some(support) if !self.images.is_empty() => check_support(self.total_bytes(), support),
            _ => Ok(()),
        }
    }

    pub fn remove_last(&mut self) -> Option<AttachedImage> {
        self.images.pop()
    }

    pub fn clear(&mut self) {
        self.images.clear();
    }

    pub fn is_empty(&self) -> bool {
        self.images.is_empty()
    }

    pub fn len(&self) -> usize {
        self.images.len()
    }

    pub fn iter(&self) -> impl Iterator<Item = &AttachedImage> {
        self.images.iter()
    }

    /// The composer's chip labels, numbered from 1 in attachment order.
    pub fn chip_labels(&self) -> Vec<String> {
        self.images
            .iter()
            .enumerate()
            .map(|(index, image)| image.chip_label(index + 1))
            .collect()
    }

    pub fn base64_images(&self) -> Vec<String> {
        self.images.iter().map(AttachedImage::base64).collect()
    }

    /// The transcript lines for these images, one per image, in order.
    pub fn transcript_lines(&self) -> Vec<String> {
        self.images
            .iter()
            .map(AttachedImage::transcript_line)
            .collect()
    }

    fn total_bytes(&self) -> usize {
        self.images.iter().map(AttachedImage::byte_size).sum()
    }
}

fn check_support(total: usize, support: ImageSupport<'_>) -> Result<(), AttachError> {
    if !support.accepts_images {
        return Err(AttachError::ModelRejectsImages {
            model: support.model.to_owned(),
        });
    }
    let limit = usize::try_from(support.max_request_bytes).unwrap_or(usize::MAX);
    if total > limit {
        return Err(AttachError::OverRequestBudget { total, limit });
    }
    Ok(())
}

/// Identify PNG, JPEG, GIF or WebP from a file's first bytes.
pub fn sniff_media_type(bytes: &[u8]) -> Option<&'static str> {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        Some("image/png")
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        Some("image/jpeg")
    } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        Some("image/gif")
    } else if bytes.len() >= 12 && &bytes[..4] == b"RIFF" && &bytes[8..12] == b"WEBP" {
        Some("image/webp")
    } else {
        None
    }
}

/// The file a paste names when the whole paste is one path to an existing file.
///
/// Terminals paste a dropped file as its path, quoted or with spaces escaped
/// (`My\ Shot.png`), sometimes as a `file://` URL. Anything else is ordinary text.
pub fn pasted_file_path(pasted: &str) -> Option<PathBuf> {
    let trimmed = pasted.trim();
    if trimmed.is_empty() || trimmed.contains('\n') {
        return None;
    }
    let unquoted = trimmed
        .strip_prefix('\'')
        .and_then(|rest| rest.strip_suffix('\''))
        .or_else(|| {
            trimmed
                .strip_prefix('"')
                .and_then(|rest| rest.strip_suffix('"'))
        });
    let text = match unquoted {
        Some(text) => text.to_owned(),
        None => unescape_shell_spaces(trimmed),
    };
    // Terminals that paste a `file://` URL percent-encode spaces and other bytes.
    let decoded;
    let text = match text.strip_prefix("file://") {
        Some(url) => {
            decoded = percent_decode(url.strip_prefix("localhost").unwrap_or(url))?;
            decoded.as_str()
        }
        None => text.as_str(),
    };
    let path = Path::new(text);
    if !path.is_absolute() && !text.starts_with("~/") {
        return None;
    }
    let path = match text.strip_prefix("~/") {
        Some(rest) => std::env::var_os("HOME").map(|home| Path::new(&home).join(rest))?,
        None => path.to_path_buf(),
    };
    path.is_file().then_some(path)
}

fn percent_decode(text: &str) -> Option<String> {
    let bytes = text.as_bytes();
    let mut decoded = Vec::with_capacity(bytes.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' {
            let hex = text.get(index + 1..index + 3)?;
            decoded.push(u8::from_str_radix(hex, 16).ok()?);
            index += 3;
        } else {
            decoded.push(bytes[index]);
            index += 1;
        }
    }
    String::from_utf8(decoded).ok()
}

fn unescape_shell_spaces(text: &str) -> String {
    let mut unescaped = String::with_capacity(text.len());
    let mut characters = text.chars();
    while let Some(character) = characters.next() {
        if character == '\\' {
            if let Some(escaped) = characters.next() {
                unescaped.push(escaped);
                continue;
            }
        }
        unescaped.push(character);
    }
    unescaped
}

fn media_label(media_type: &str) -> &str {
    media_type.strip_prefix("image/").unwrap_or(media_type)
}

fn megabytes(bytes: usize) -> String {
    format!("{:.1} MB", bytes as f64 / 1_000_000.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn png(width: usize, height: usize) -> AttachedImage {
        AttachedImage::from_rgba(width, height, &vec![255; width * height * 4]).unwrap()
    }

    fn vision(max_request_bytes: u64) -> ImageSupport<'static> {
        ImageSupport {
            model: "vision-model",
            accepts_images: true,
            max_request_bytes,
        }
    }

    #[test]
    fn clipboard_image_encodes_png() {
        let image = png(3, 2);
        let translucent = AttachedImage::from_rgba(1, 1, &[0, 0, 0, 128]).unwrap();

        assert_eq!(sniff_media_type(&image.bytes), Some("image/png"));
        // The IHDR colour type: 2 is RGB for opaque images, 6 keeps alpha.
        assert_eq!(image.bytes[25], 2);
        assert_eq!(translucent.bytes[25], 6);
        assert_eq!(image.chip_label(1), "Image 1 · 3×2");
        assert_eq!(image.transcript_line(), "[image 3×2 png]");
    }

    #[test]
    fn short_clipboard_buffer_is_refused() {
        assert_eq!(
            AttachedImage::from_rgba(2, 2, &[0; 12]),
            Err(AttachError::EncodeFailed)
        );
    }

    #[test]
    fn file_urls_are_percent_decoded() {
        let directory = std::env::temp_dir().join(format!("wisp-url-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("My Shot.png");
        std::fs::write(&path, &*png(1, 1).bytes).unwrap();
        let url = format!("file://{}", path.display().to_string().replace(' ', "%20"));

        assert_eq!(pasted_file_path(&url), Some(path.clone()));
        assert_eq!(pasted_file_path("file:///bad%2"), None);
        assert_eq!(pasted_file_path("~someone/shot.png"), None);
        std::fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn large_non_image_reads_only_its_header() {
        let directory = std::env::temp_dir().join(format!("wisp-log-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("huge.log");
        std::fs::write(&path, vec![b'x'; MAX_IMAGE_BYTES * 2]).unwrap();

        assert_eq!(AttachedImage::from_file(&path), Ok(None));
        std::fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn attach_enforces_count_limit() {
        let mut images = ComposerImages::default();
        for _ in 0..MAX_IMAGES {
            images.attach(png(1, 1), None).unwrap();
        }

        assert_eq!(images.attach(png(1, 1), None), Err(AttachError::TooMany));
        assert_eq!(images.len(), MAX_IMAGES);
    }

    #[test]
    fn attach_respects_model_support() {
        let mut images = ComposerImages::default();
        let text_only = ImageSupport {
            model: "text-model",
            accepts_images: false,
            max_request_bytes: 0,
        };

        assert_eq!(
            images.attach(png(1, 1), Some(text_only)),
            Err(AttachError::ModelRejectsImages {
                model: "text-model".into()
            })
        );
        let size = png(1, 1).byte_size() as u64;
        images.attach(png(1, 1), Some(vision(size))).unwrap();
        assert!(matches!(
            images.attach(png(1, 1), Some(vision(size))),
            Err(AttachError::OverRequestBudget { .. })
        ));
        assert_eq!(images.len(), 1);
        assert!(images.check(Some(text_only)).is_err());
        assert_eq!(images.check(None), Ok(()));
    }

    #[test]
    fn file_paths_attach_only_images() {
        let directory = std::env::temp_dir().join(format!("wisp-images-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let image_path = directory.join("My Shot.png");
        std::fs::write(&image_path, &*png(3, 2).bytes).unwrap();
        let text_path = directory.join("notes.txt");
        std::fs::write(&text_path, "not an image").unwrap();

        let escaped = image_path.display().to_string().replace(' ', "\\ ");
        let quoted = format!("'{}'", image_path.display());
        let url = format!("file://{}", image_path.display());
        for pasted in [escaped.as_str(), quoted.as_str(), url.as_str()] {
            let path = pasted_file_path(pasted).expect(pasted);
            let image = AttachedImage::from_file(&path).unwrap().unwrap();
            assert_eq!(image.chip_label(2), "Image 2 · My Shot.png");
        }
        let text = pasted_file_path(&text_path.display().to_string()).unwrap();
        assert_eq!(AttachedImage::from_file(&text).unwrap(), None);
        assert_eq!(pasted_file_path("look at this"), None);
        assert_eq!(pasted_file_path("relative/shot.png"), None);
        assert_eq!(
            pasted_file_path(&format!("{}\nsecond line", image_path.display())),
            None
        );
        std::fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn oversized_file_is_refused() {
        let directory =
            std::env::temp_dir().join(format!("wisp-images-large-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let path = directory.join("huge.png");
        let mut bytes = b"\x89PNG\r\n\x1a\n".to_vec();
        bytes.resize(MAX_IMAGE_BYTES + 1, 0);
        std::fs::write(&path, &bytes).unwrap();

        assert!(matches!(
            AttachedImage::from_file(&path),
            Err(AttachError::TooLarge(size)) if size == MAX_IMAGE_BYTES + 1
        ));
        std::fs::remove_dir_all(&directory).unwrap();
    }

    #[test]
    fn limits_match_the_command_schema() {
        let schema: serde_json::Value = serde_json::from_str(include_str!(
            "../../../schemas/live-rpc/rust-commands.schema.json"
        ))
        .unwrap();
        let text = schema.to_string();
        // The base64 of the largest image, and the per-message image count.
        let base64_limit = MAX_IMAGE_BYTES.div_ceil(3) * 4;
        assert!(
            text.contains(&format!("\"maxLength\":{base64_limit}")),
            "base64 limit"
        );
        assert!(
            text.contains(&format!("\"maxItems\":{MAX_IMAGES}")),
            "image count limit"
        );
    }
}

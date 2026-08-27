#![forbid(unsafe_code)]

#[tokio::main]
async fn main() {
    if let Err(error) = wisp_tui::run_from_env().await {
        eprintln!("{}", wisp_tui::render_top_level_error(&error));
        std::process::exit(1);
    }
}

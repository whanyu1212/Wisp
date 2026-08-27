use clap::Parser;
use std::ffi::OsString;

#[derive(Debug, Parser)]
#[command(version, about = "Minimal native frontend for the Wisp RPC backend")]
pub struct Cli {
    /// Exact installed Wisp package version required from the backend.
    #[arg(long)]
    pub expected_backend_version: String,

    /// Exact backend executable and arguments, supplied after `--`.
    #[arg(last = true, required = true, num_args = 1.., allow_hyphen_values = true)]
    pub backend: Vec<OsString>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preserves_opaque_backend_argv_after_separator() {
        let cli = Cli::try_parse_from([
            "wisp-tui",
            "--expected-backend-version",
            "0.9.0",
            "--",
            "/exact/python",
            "-m",
            "wisp",
            "--mode",
            "rpc",
        ])
        .unwrap();
        assert_eq!(cli.expected_backend_version, "0.9.0");
        assert_eq!(
            cli.backend,
            ["/exact/python", "-m", "wisp", "--mode", "rpc"]
                .map(OsString::from)
                .to_vec()
        );
    }
}

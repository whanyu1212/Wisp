# Request timeouts

A service's effective request timeout, in seconds, for one environment and region:

1. Start from the default for the service's tier (`tier` in `services/<name>.ini`), in
   `config/base.ini`.
2. If the environment sets a timeout for the service, it replaces the default. A region file,
   `env/<environment>/regions/<region>.ini`, takes precedence over the environment-wide
   `env/<environment>/overrides.ini`.
3. Multiply by the region's factor for the service's tier, in `config/regions.ini`.

Services listed in `env/<environment>/disabled.txt` do not run in that environment.

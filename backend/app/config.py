from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TAOSCOPE_", extra="ignore")

    database_url: str = "postgresql://taoscope:taoscope@db:5432/taoscope"

    # chain
    network: str = "finney"
    chain_endpoint: str = ""          # optional explicit ws:// endpoint

    # cadences (seconds)
    fast_poll: int = 12               # ~1 block: prices/pools -> live state + websocket
    snapshot_every: int = 60          # persist a subnet_snapshot row
    neuron_poll: int = 900            # full metagraph sweep (30k neurons)
    price_poll: int = 300             # TAO/USD

    # auth
    jwt_secret: str = "dev-insecure-change-me"
    cookie_secure: bool = False          # flip on once TLS is live
    public_url: str = ""                 # e.g. https://tao.example.com (required for Google)
    google_client_id: str = ""
    google_client_secret: str = ""
    allowed_emails: str = ""             # comma-separated seed for the allowlist
    max_login_attempts: int = 8

    # telegram (long-polling, so it needs no public URL)
    telegram_bot_token: str = ""
    # Create a forum topic for every subnet our coldkeys hold a UID on, in every
    # linked chat that was set up with /setup. Only ever adds topics.
    telegram_auto_topics: bool = True

    # competition tracking (off-chain, per-subnet dashboards)
    comp_enabled: bool = True
    # optional: raises the GitHub repo-watch limit from 60/h to 5000/h.
    # Conditional ETag requests keep us under 60/h without it.
    github_token: str = ""

    # natural-language Q&A in Telegram.
    # Either a direct Anthropic key, or an OpenRouter key (OpenRouter serves the
    # Anthropic Messages format, so the same SDK and tool-calling code works).
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api"
    ai_model: str = "claude-opus-5"
    ai_effort: str = "medium"        # low | medium | high | xhigh | max
    ai_daily_limit: int = 100        # answered questions per chat per 24h
    jwt_ttl_hours: int = 720
    admin_email: str = ""
    admin_password: str = ""

    # ---- API credit monitoring -------------------------------------------
    # Running a provider dry kills a run mid-flight, so these are watched the
    # same way a competition is: poll, diff, alert only on a threshold crossing.
    # A provider with no key configured is simply skipped, never reported.
    lium_api_key: str = ""
    lium_api_base: str = "https://lium.io/api"   # from Lium's own SDK
    chutes_api_key: str = ""
    parallel_api_key: str = ""
    # Optional: a JWT from Parallel's OAuth device flow. Their balance
    # endpoint refuses plain API keys, so without this we report liveness only.
    parallel_oauth_token: str = ""
    vercel_api_token: str = ""
    vercel_team_id: str = ""
    credits_enabled: bool = True
    # Which providers to poll and show. Parallel and Vercel are implemented and
    # verified, but neither can report a balance -- Parallel's balance endpoint
    # refuses API keys (OAuth only) and Vercel bills per plan with no prepaid
    # credit -- so they are off by default rather than sitting in /keys as two
    # permanent "n/a" rows. Add them back here to re-enable.
    credits_providers: str = "lium,openrouter,chutes"
    credits_poll: int = 900                       # 15 min
    # Balances that deserve a message, tightest-last. An alert fires once per
    # crossing, not once per poll.
    credits_thresholds: str = "100,50,20,10,5,1"

    tao_price_url: str = (
        "https://api.coingecko.com/api/v3/simple/price"
        "?ids=bittensor&vs_currencies=usd&include_24hr_change=true"
    )


settings = Settings()

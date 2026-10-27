"""Runtime configuration, read from environment variables (or a local .env file)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Database (Neon) ---------------------------------------------------
    database_url: str  # postgresql://user:pass@ep-xxx-pooler.<region>.aws.neon.tech/db?sslmode=require

    # --- Security ----------------------------------------------------------
    # Fernet key used to encrypt WhatsApp access tokens at rest.
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    encryption_key: str = ""
    session_days: int = 30

    # Parity with the original app: the UI's "Forgot password" screen only asks for email + new password.
    # That lets anybody reset anybody's password. Set to false once you add an emailed-token flow.
    insecure_password_reset: bool = False

    # Comma-separated emails allowed to upload / re-point the Windows installer.
    # Empty = any logged-in user (original behaviour).
    installer_admin_emails: str = ""

    # --- App ---------------------------------------------------------------
    seed_demo_data: bool = True  # creates demo@manweta.ai / password123 (set false in real production)
    public_base_url: str = ""  # e.g. https://manweta.azurewebsites.net ; empty = derive from request
    enable_docs: bool = False  # exposes /api/docs when true

    # --- Meta / WhatsApp ---------------------------------------------------
    meta_app_id: str = ""
    meta_app_secret: str = ""  # if set, webhook POSTs must carry a valid X-Hub-Signature-256
    meta_embedded_signup_config_id: str = ""
    meta_embedded_signup_version: str = "v4"
    meta_webhook_verify_token: str = ""  # must be set; an empty token rejects every webhook verification
    meta_graph_api_version: str = "v25.0"
    meta_template_name: str = "manweta_payment_reminder"
    meta_template_language: str = "en_US"
    meta_phone_registration_pin: str = ""
    meta_auto_register_phone: bool = True
    meta_http_timeout_seconds: float = 20.0
    # Temporary, tightly restricted App Review demo sender. Keep these server-side only.
    meta_demo_access_token: str = ""
    meta_demo_phone_number_id: str = ""
    meta_demo_recipient: str = ""

    # --- Windows installer / Azure Blob Storage ----------------------------
    windows_installer_version: str = "v1.4.2"
    windows_installer_blob_url: str = ""  # optional external https URL (same as original)
    azure_storage_connection_string: str = ""  # either this ...
    azure_storage_account_url: str = ""  # ... or this (https://<acct>.blob.core.windows.net) + managed identity
    azure_storage_container: str = "installers"
    sas_ttl_minutes: int = 15
    max_installer_mb: int = 150

    @property
    def installer_admins(self) -> set[str]:
        return {e.strip().lower() for e in self.installer_admin_emails.split(",") if e.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

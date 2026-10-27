from createto_info.config import load_config
from createto_info.db.session import sqlalchemy_url


def test_config_loads_and_has_no_secrets():
    cfg = load_config()
    assert cfg.models.gate == "baseten/mammoth-1:free"
    assert "a-us.storyblok.com" in cfg.scope.asset_hosts
    assert cfg.storage.raw_dir.is_absolute()
    assert "api_key" not in str(cfg.snapshot())


def test_sqlalchemy_url_uses_psycopg3():
    assert sqlalchemy_url("postgresql://u:p@h/db") == "postgresql+psycopg://u:p@h/db"
    assert sqlalchemy_url("postgres://u:p@h/db") == "postgresql+psycopg://u:p@h/db"

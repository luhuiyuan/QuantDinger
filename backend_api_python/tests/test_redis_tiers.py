from app.config.redis_urls import cache_key, cache_redis_url


def test_cache_redis_url_uses_cache_endpoint(monkeypatch):
    monkeypatch.setenv("REDIS_HOST", "cache")
    monkeypatch.setenv("REDIS_PASSWORD", "cache password")
    monkeypatch.delenv("REDIS_URL", raising=False)

    assert cache_redis_url() == "redis://:cache%20password@cache:6379/0"


def test_explicit_redis_urls_take_precedence(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://cache.example/4")

    assert cache_redis_url() == "redis://cache.example/4"


def test_cache_key_has_versioned_namespace(monkeypatch):
    monkeypatch.delenv("REDIS_CACHE_NAMESPACE", raising=False)
    assert cache_key("market:BTCUSDT") == "quantdinger:cache:v1:market:BTCUSDT"

    monkeypatch.setenv("REDIS_CACHE_NAMESPACE", "tenant-a:v2")
    assert cache_key("market:BTCUSDT") == "tenant-a:v2:market:BTCUSDT"

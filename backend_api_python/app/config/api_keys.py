"""
API key configuration.
All third-party keys should be provided via environment variables (recommended: backend_api_python/.env).
"""
import os

class MetaAPIKeys(type):
    """Metaclass that supports dynamic class-level API key lookup."""
    
    @property
    def OPENROUTER_API_KEY(cls):
        # Always check env var first to avoid stale cache issues
        env_val = os.getenv('OPENROUTER_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('openrouter', {}).get('api_key')
        return val if val else ''
    
    @property
    def OPENAI_API_KEY(cls):
        """OpenAI direct API key"""
        env_val = os.getenv('OPENAI_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('openai', {}).get('api_key')
        return val if val else ''
    
    @property
    def GOOGLE_API_KEY(cls):
        """Google Gemini API key"""
        env_val = os.getenv('GOOGLE_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('google', {}).get('api_key')
        return val if val else ''
    
    @property
    def DEEPSEEK_API_KEY(cls):
        """DeepSeek API key"""
        env_val = os.getenv('DEEPSEEK_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('deepseek', {}).get('api_key')
        return val if val else ''
    
    @property
    def GROK_API_KEY(cls):
        """xAI Grok API key"""
        env_val = os.getenv('GROK_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('grok', {}).get('api_key')
        return val if val else ''

    @property
    def ATLASCLOUD_API_KEY(cls):
        """AtlasCloud API key"""
        env_val = os.getenv('ATLASCLOUD_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('atlascloud', {}).get('api_key')
        return val if val else ''

    @property
    def CUSTOM_API_KEY(cls):
        """Custom LLM API key (for OpenAI-compatible custom endpoints)"""
        env_val = os.getenv('CUSTOM_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('custom', {}).get('api_key')
        return val if val else ''

    @property
    def CUSTOM_API_URL(cls):
        """Custom LLM API base URL (e.g., https://your-api.com/v1)"""
        env_val = os.getenv('CUSTOM_API_URL', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('custom', {}).get('base_url')
        return val if val else ''

    @property
    def CUSTOM_MODEL(cls):
        """Custom LLM model name"""
        env_val = os.getenv('CUSTOM_MODEL', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('custom', {}).get('model')
        return val if val else ''

    @property
    def MINIMAX_API_KEY(cls):
        """MiniMax API key"""
        env_val = os.getenv('MINIMAX_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('minimax', {}).get('api_key')
        return val if val else ''

    @property
    def LITELLM_API_KEY(cls):
        """LiteLLM API key (optional, litellm reads provider env vars automatically)"""
        env_val = os.getenv('LITELLM_API_KEY', '').strip()
        if env_val:
            return env_val
        from app.utils.config_loader import load_addon_config
        val = load_addon_config().get('litellm', {}).get('api_key')
        return val if val else ''
    

class APIKeys(metaclass=MetaAPIKeys):
    """API key configuration."""
    
    @classmethod
    def get(cls, key_name: str, default: str = '') -> str:
        """Return an API key by name."""
        if hasattr(cls, key_name):
            return getattr(cls, key_name)
        return default
    
    @classmethod
    def is_configured(cls, key_name: str) -> bool:
        """Return whether an API key is configured."""
        value = cls.get(key_name)
        return bool(value and value.strip())

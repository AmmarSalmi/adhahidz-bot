import os

def get_proxy_url() -> str | None:
    """Constructs the proxy URL from environment variables.
    
    Expected format: http://user:pass@host:port
    """
    user = os.getenv("PROXY_USER")
    password = os.getenv("PROXY_PASS")
    host = os.getenv("PROXY_HOST", "gw.databay.co")
    port = os.getenv("PROXY_PORT", "8888")

    if not user or not password:
        return None

    return f"http://{user}:{password}@{host}:{port}"

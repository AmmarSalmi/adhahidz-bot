import os

def get_proxy_url(session_id: str | None = None) -> str | None:
    """Constructs the proxy URL from environment variables.
    
    Expected format: http://user:pass@host:port
    Supports Databay sessionId parameter for sticky sessions.
    """
    user = os.getenv("PROXY_USER")
    password = os.getenv("PROXY_PASS")
    host = os.getenv("PROXY_HOST", "gw.databay.co")
    port = os.getenv("PROXY_PORT", "8888")

    if not user or not password:
        return None

    # Append Databay sticky session parameter if requested
    if session_id:
        user = f"{user}-sessionId-{session_id}"

    return f"http://{user}:{password}@{host}:{port}"

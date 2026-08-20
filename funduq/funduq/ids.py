import secrets


def new_id(category: str) -> str:
    return f"{category}_{secrets.token_hex(12)}"

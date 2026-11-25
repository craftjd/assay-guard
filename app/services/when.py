"""Short local timestamps for the screens."""


def friendly(value) -> str:
    if value is None or not hasattr(value, "strftime"):
        return ""
    hour = int(value.strftime("%I"))
    return f"{value.strftime('%b')} {value.day}, {hour}:{value.strftime('%M')} {value.strftime('%p')}"

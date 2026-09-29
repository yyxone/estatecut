"""Read-only defaults shipped inside the installed wheel."""
from pathlib import Path

def resource_path(name: str) -> Path:
    if Path(name).name != name:
        raise ValueError('Resource name must be a basename')
    return Path(__file__).resolve().with_name('resources') / name

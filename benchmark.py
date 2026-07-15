from pathlib import Path
from azure_openai_cache_benchmark.cli import main


if __name__ == "__main__":
    raise SystemExit(main(default_base=Path(__file__).resolve().parent))

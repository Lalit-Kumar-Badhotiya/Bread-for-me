"""Entry point: python run_apply.py <command> ..."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from bot.main import main
    main()

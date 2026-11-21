"""The launchd plist template's placeholders must all be covered by the installer."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "voice_mode"


def test_installer_substitutes_every_plist_placeholder():
    template = (ROOT / "templates/launchd/com.voicemode.whisper.plist").read_text()
    installer = (ROOT / "tools/whisper/install.py").read_text()
    for token in set(re.findall(r"\{[A-Z_]+\}", template)):
        assert f'replace("{token}"' in installer, f"{token} is never substituted"

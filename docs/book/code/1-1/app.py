"""Chapter 1-1: is featherweight installed, and is there a key? No model is called."""

import os

from featherweight import __version__

print("featherweight", __version__)
for key in ("GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
    print(key, "is set" if os.environ.get(key) else "is not set")

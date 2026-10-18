# Live Gemini audio fixtures

The WAV files in this folder are short synthetic speech samples generated offline with Windows `System.Speech` and the installed Microsoft Zira Desktop `en-US` voice. The three scripts are maintenance-note examples and are not recordings of technicians or machines. Only an English voice was available in the offline TTS inventory, so multilingual structuring is **not verified** by these fixtures.

`pytest -m live` or `python scripts/verify_gemini.py` sends these clips to the configured Gemini model when credentials are available. Live calls can incur API charges. The files are PCM WAV.

# Windows setup

## 1. Install Python
Install Python 3.10 or newer and confirm:

```powershell
python --version
```

## 2. Install dependencies

```powershell
python -m pip install -r requirements.txt
```

## 3. Configure AI (optional)
Copy `.env.example` to `.env` and add a fresh Gemini API key. Keep `.env` private.

The app works without a key using deterministic local fallbacks.

## 4. Start

```powershell
python server.py
```

Then open:

```text
http://localhost:3000
```

## 5. Data
The first login creates `karmayogi.db`. Delete that file to reset the local demo dataset.

## External learning integration
The roadmap uses a normalized prototype catalogue. It is intentionally separated from the frontend so a future approved authenticated adapter can replace it without changing the user experience.

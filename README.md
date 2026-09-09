# Karmayogi AI — Competency Intelligence

A self-contained local application for role-aware competency intelligence, AI-assisted assessments, topic-wise scoring, practical workplace simulations, explainable learning recommendations, persistent progress analytics, an AI tutor with conversation memory, document extraction, and meaningful gamification.

## Run on Windows
1. Install Python 3.10+.
2. Open this folder in PowerShell.
3. Optional: create `.env` from `.env.example` and add a fresh Gemini API key to `GEMINI_API_KEY`.
4. Run `python server.py` or double-click `run.bat`.
5. Open http://localhost:3000

## AI key
The key belongs on the backend only. Do not commit `.env` or expose the key in frontend code. The application remains usable in local fallback mode without a key.

The default low-cost model is `gemini-3.5-flash-lite`; the backend also discovers an available Flash-family model for the configured key when needed.

## Supported document extraction
PDF (PyPDF), DOCX (python-docx), TXT and MD. Large inputs are bounded and the assessment flow selects relevant text rather than blindly sending a full document.

## Persistence
SQLite is created automatically as `karmayogi.db`. It stores users, competency evidence, chat sessions/messages, learning materials, assessments, attempts, responses, recommendations, learning actions, quests, skill events, simulations, and spaced-review schedules.

## Learning loop
Role → competency profile → diagnostic assessment → skill gap → personalized learning → workplace practice → reassessment → evidence update → next recommendation.

## External learning integration boundary
The roadmap uses a normalized local catalogue so the frontend can remain stable while an approved authenticated external learning adapter is introduced. No protected endpoint is scraped and no unavailable API is fabricated.

## Main product areas
- Overview cockpit
- Competency loadout and evidence
- AI assessment generation and validation
- Persistent assessment history and review
- Explainable learning roadmap
- Workplace simulations with rubric-based feedback
- AI Tutor with persistent conversations and bounded context
- Evidence and progress analytics
- Workforce aggregate view with representative local data labeling
- Server-side AI connection settings
- Meaningful quests, XP and learning streak

## Production roadmap
- proper authentication and role-based access control
- PostgreSQL / managed persistence
- authenticated external learning integration
- OCR for scanned PDFs
- semantic retrieval / embeddings
- audit logging, encryption, secrets management and security testing
- multilingual support
- deeper organizational analytics and notifications

# Wood Press 0.3

Pierwsza wersja, w której frontend i API działają jako jedna aplikacja Flask.

## Uruchomienie lokalne

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

Potem otwórz `http://127.0.0.1:5000`.

## Jak działa „Odśwież”

Przycisk wysyła `POST /api/refresh`. Serwer uruchamia silnik RSS + pobieranie wskazanych stron, zapisuje nowe materiały w SQLite, a następnie frontend pobiera `/api/news`.

## Hosting

Projekt zawiera `render.yaml` i jest przygotowany do uruchomienia jako Python Web Service. Render dokumentuje uruchamianie Flask przez Gunicorn i automatyczne wdrażanie z repozytorium Git.

**Uwaga:** domyślny system plików usług hostingowych może być nietrwały. SQLite jest więc na tym etapie magazynem wersji 0.3 do prototypu; później warto przenieść bazę do PostgreSQL.

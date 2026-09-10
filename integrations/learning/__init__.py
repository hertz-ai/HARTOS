"""Learning / book-navigation agent surface.

Wraps the EXISTING local book pipeline (Nunba routes/upload_routes.py +
routes/db_routes.py, tables pdf_files + page_layouts) as LLM tools so an
agent can drive page-wise and chapter-wise learning during a /chat turn.

No new endpoints and no new storage — see book_tools.py for the contract.
"""

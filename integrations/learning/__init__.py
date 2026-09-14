"""Learning / book agent surface.

  book_pipeline  the ONE PDF book pipeline -- parse, chapters, page images,
                 storage -- which every HARTOS node runs
  api_books      its HTTP routes, registered in integrations.social.init_social
  book_tools     the LLM tools an agent teaches from, calling book_pipeline
                 in-process
"""

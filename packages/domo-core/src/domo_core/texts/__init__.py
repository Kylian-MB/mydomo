"""The texts people read: whatever the code shows someone goes through a ``Texts`` (``view``),
never a language. Once Domo is installed, they are the texts he wrote for their reader
(``view.RecueilTexts``, read for each request by ``web.texts_middleware``, written in the
background by ``scheduler``); during the installation, the fixed catalogs (``CatalogTexts``). What
models read is the English catalog (``REFERENCE``). Dates and amounts are put in words by the same
``Texts`` (``formatting``).
"""

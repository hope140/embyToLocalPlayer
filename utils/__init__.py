"""ETLP runtime package for the ``utils`` modules.

Keeping this ``__init__.py`` makes ``utils`` a regular package.  Without it
the directory is only a namespace package, and a third-party PyPI package
also named ``utils`` installed in site-packages shadows it, breaking
``python embyToLocalPlayer.py`` with ``No module named 'utils.*'``.
"""

# -*- coding: utf-8 -*-
# License: GPLv3 Copyright: 2021, winnbyte
store_version = 8  # Needed for dynamic plugin loading

__license__ = "GPLv3"
__copyright__ = "poochinski9"
__docformat__ = "restructuredtext en"

from calibre.customize import StoreBase


class LibgenStore(StoreBase):
    name = "Library Genesis"
    version = (2, 0, 0)
    description = (
        "Searches Library Genesis, Z-Library and Anna\u2019s Archive for free e-books. "
        "Configurable mirrors and per-source toggles available in plugin preferences."
    )
    author = "poochinski9"
    drm_free_only = True
    actual_plugin = "calibre_plugins.store_libgen.libgen_plugin:LibgenStorePlugin"
    formats = ["EPUB", "PDF", "MOBI", "AZW3", "DJVU", "FB2"]

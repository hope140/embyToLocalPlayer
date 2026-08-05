# Bundled third-party dependency

This directory carries an unmodified distribution wheel for the optional
watch-together WebSocket client.  `embyToLocalPlayer` is not the upstream
`websocket-client` project; the wheel is only a fallback for installations
where the user has not installed that dependency separately.

- Package/version: `websocket-client==1.8.0`
- Wheel: `websocket_client-1.8.0-py3-none-any.whl`
- Source (official PyPI file):
  <https://files.pythonhosted.org/packages/5a/84/44687a29792a70e111c5c477230a72c4b957d88d16141199bf9acb7537a3/websocket_client-1.8.0-py3-none-any.whl>
- PyPI release metadata: <https://pypi.org/project/websocket-client/1.8.0/>
- SHA256:
  `17b44cc997f5c498e809b22cdf2d9c7a9e71c02c8cc2b6c56e7c2d1239bfa526`
- License: Apache-2.0 (the original license text is included in the wheel at
  `websocket_client-1.8.0.dist-info/LICENSE`; the metadata declares
  `License: Apache-2.0`).

The runtime verifies this exact SHA256 before adding the wheel to `sys.path`.
It never installs the wheel, runs package scripts, or downloads anything at
runtime.  A normal system/user `websocket-client` import remains preferred.

## Upgrading

Do not edit the wheel by hand.  To update it, download the `py3-none-any`
wheel for the intended version directly from the matching official PyPI
release, verify it with `Get-FileHash -Algorithm SHA256` (or
`hashlib.sha256`), replace the file unchanged, and update the filename,
hard-coded digest in `utils/watch_together_client.py`, tests, and this note.
Keep `requirements.txt` pinned to the same version until the replacement has
been reviewed.

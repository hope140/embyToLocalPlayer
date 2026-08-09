# Bundled third-party dependency

This directory carries an unmodified distribution wheel for the optional
Emby remote-control WebSocket client.  `embyToLocalPlayer` is not the upstream
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

The beta Windows package also carries compatible CPython 3.9 x86 wheels for
the CloudDrive2 and HTTP client dependencies (`grpcio`, `protobuf`, `requests`,
`typing-extensions`, `certifi`, `charset-normalizer`, `idna`, and `urllib3`).
`utils/dependency_bootstrap.py` extracts these wheels into a private cache on
first launch because binary extensions cannot be imported directly from a zip
archive.  The cache is machine-generated and is intentionally excluded from
source packages.

The HTTP dependency wheels are pinned to these releases and are downloaded
unchanged from the official PyPI file host:

- `requests==2.32.5`: [wheel](https://files.pythonhosted.org/packages/1e/db/4254e3eabe8020b458f1a747140d32277ec7a271daf1d235b70dc0b4e6e3/requests-2.32.5-py3-none-any.whl), SHA256 `2462f94637a34fd532264295e186976db0f5d453d1cdd31473c85a6a161affb6`
- `urllib3==2.6.3`: [wheel](https://files.pythonhosted.org/packages/39/08/aaaad47bc4e9dc8c725e68f9d04865dbcb2052843ff09c97b08904852d84/urllib3-2.6.3-py3-none-any.whl), SHA256 `bf272323e553dfb2e87d9bfd225ca7b0f467b919d7bbd355436d3fd37cb0acd4`
- `idna==3.15`: [wheel](https://files.pythonhosted.org/packages/d2/23/408243171aa9aaba178d3e2559159c24c1171a641aa83b67bdd3394ead8e/idna-3.15-py3-none-any.whl), SHA256 `048adeaf8c2d788c40fee287673ccaa74c24ffd8dcf09ffa555a2fbb59f10ac8`

## Upgrading

Do not edit a wheel by hand.  To update one, download the `py3-none-any` wheel
for the intended version directly from the matching official PyPI release,
verify it with `Get-FileHash -Algorithm SHA256` (or `hashlib.sha256`), replace
the file unchanged, and update the filename, digest mapping in
`utils/dependency_bootstrap.py`, tests, and this note.  Keep `requirements.txt`
pinned to the same version until the replacement has been reviewed.

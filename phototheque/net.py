"""Accès réseau HTTPS commun (Pl@ntNet, WordPress, téléchargement des modèles)."""
from __future__ import annotations

import io
import ssl
import urllib.request
import uuid

_SSL_CONTEXT: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """Contexte TLS qui vérifie les certificats avec le magasin du système (Windows, macOS, Linux).

    Le Python de python.org sous Windows ne lit pas le magasin de certificats de Windows, d'où
    « CERTIFICATE_VERIFY_FAILED » ; c'est aussi le cas derrière un antivirus ou un proxy d'entreprise
    qui inspecte HTTPS avec son propre certificat (installé dans Windows). truststore utilise le
    magasin du système ; à défaut, certifi (certificats de Mozilla). La vérification reste active."""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        try:
            import truststore
            _SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except Exception:
            try:
                import certifi
                _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
            except Exception:
                _SSL_CONTEXT = ssl.create_default_context()
    return _SSL_CONTEXT


def open_url(req, timeout: int):
    return urllib.request.urlopen(req, timeout=timeout,
                                  context=ssl_context() if str(getattr(req, "full_url", req)).startswith("https") else None)



def multipart(fields: list[tuple[str, str]], files: list[tuple[str, str, bytes, str]]) -> tuple[bytes, str]:
    """Corps multipart/form-data : champs texte + fichiers (nom du champ, nom du fichier, octets, type)."""
    boundary = uuid.uuid4().hex
    out = io.BytesIO()
    for name, value in fields:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        out.write(str(value).encode("utf-8"))
        out.write(b"\r\n")
    for name, filename, data, ctype in files:
        out.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        out.write(data)
        out.write(b"\r\n")
    out.write(f"--{boundary}--\r\n".encode())
    return out.getvalue(), f"multipart/form-data; boundary={boundary}"

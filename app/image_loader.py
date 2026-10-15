from __future__ import annotations

import hashlib
import io
from pathlib import Path

import requests
from PIL import Image


class ImageLoadError(RuntimeError):
    """Erro controlado ao baixar ou abrir imagem."""


class CachedImageLoader:
    """Baixa imagens e mantém cache local para acelerar aberturas futuras."""

    def __init__(self, cache_dir: str | Path = "data/cards_cache", timeout: int = 20) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.session = requests.Session()

    def load_pil_image(self, url: str) -> Image.Image:
        if not url:
            raise ImageLoadError("URL da imagem não informada.")

        cache_path = self._cache_path(url)

        try:
            if cache_path.exists() and cache_path.stat().st_size > 0:
                return Image.open(cache_path).convert("RGBA")

            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()

            image = Image.open(io.BytesIO(response.content)).convert("RGBA")
            image.save(cache_path, format="PNG")
            return image

        except requests.Timeout as exc:
            raise ImageLoadError("Tempo limite excedido ao carregar imagem.") from exc
        except requests.RequestException as exc:
            raise ImageLoadError(f"Falha ao carregar imagem: {exc}") from exc
        except OSError as exc:
            raise ImageLoadError("Arquivo de imagem inválido ou corrompido.") from exc

    def _cache_path(self, url: str) -> Path:
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.png"

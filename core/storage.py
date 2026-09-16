import logging

from whitenoise.storage import CompressedManifestStaticFilesStorage, MissingFileError

logger = logging.getLogger(__name__)


class TolerantManifestStaticFilesStorage(CompressedManifestStaticFilesStorage):
    """
    CompressedManifestStaticFilesStorage tolerante a referências CSS não resolvidas.

    Quando o post_process do Django não consegue resolver uma URL referenciada em
    CSS (ex: @import url('widgets.css') em forms.css do Admin), em vez de levantar
    MissingFileError e abortar o deploy, registra um warning e continua.
    """

    # Desativa o strict mode do manifesto — URLs não encontradas retornam o path
    # original sem hash em vez de levantar ValueError.
    manifest_strict = False

    def post_process(self, *args, **kwargs):
        for name, hashed_name, processed in super().post_process(*args, **kwargs):
            if isinstance(processed, (MissingFileError, ValueError)):
                # Log para visibilidade mas não aborta o deploy
                logger.warning(
                    "collectstatic: não foi possível resolver referência em '%s': %s. "
                    "O arquivo será servido sem hash de nome (seguro para assets do Django Admin).",
                    name,
                    processed,
                )
                # Yield como processado=False (skip) em vez de uma Exception
                yield name, hashed_name, False
            else:
                yield name, hashed_name, processed
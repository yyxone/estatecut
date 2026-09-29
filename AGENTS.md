# Estatecut

This checkout contains the public Python source and synthetic tests.
Never discover, modify, or migrate an existing user installation as part of repository work.
Do not import private media, credentials, databases, personal music inventories, or runtime outputs.
Preserve source media; outputs must stay outside the input media directory.
Use FFmpeg/FFprobe with argument arrays and shell=False.
Tests use synthetic media and mock ASR; do not contact providers or download models.
Preserve transcript, cut, and subtitle review locks and truthful QA failure states.
Run `python -B -m pytest -q -p no:cacheprovider` from this directory.
Validate the built wheel outside the source tree before a release.
Publishing and changes to existing installations must remain within the user's explicit task scope.

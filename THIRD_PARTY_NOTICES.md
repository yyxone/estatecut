# Third-party acknowledgements

Estatecut includes independently maintained Python workflows and acknowledges the following upstream influences. Their MIT copyright and permission notices are retained in full in `licenses/` and included in distributions.

## browser-use/video-use

Timeline visualization (`talkcut/timeline_view.py`), self-evaluation (`talkcut/self_eval.py`), HDR tone mapping (`talkcut/cut.py`), and loudness/export patterns (`talkcut/qaexport.py`) were informed by this project. The original local evaluation is dated July 1, 2026, and the related Estatecut implementation was committed July 4, 2026. We do not claim these ideas are novel or imply upstream endorsement.

- Repository: https://github.com/browser-use/video-use
- License reference: https://github.com/browser-use/video-use/blob/92c2b34e44c205cbc2acae7f6ca7c1c219d5dd66/LICENSE
- MIT, Copyright (c) 2026 Browser Use; full notice: [licenses/video-use-LICENSE.txt](licenses/video-use-LICENSE.txt).

## harry0703/MoneyPrinterTurbo

Punctuation-aware subtitle grouping (`talkcut/subtitle.py`) records influence from this project.

- Repository: https://github.com/harry0703/MoneyPrinterTurbo
- License reference: https://github.com/harry0703/MoneyPrinterTurbo/blob/d72fda40c4bbe6b7cdf2318692308827a5acc967/LICENSE
- MIT, Copyright (c) 2024 Harry; full notice: [licenses/MoneyPrinterTurbo-LICENSE.txt](licenses/MoneyPrinterTurbo-LICENSE.txt).

These pinned references establish the upstream license at or before the recorded evaluation/source baseline dates. They are not assertions that those exact revisions were imported. Conservative attribution is retained whether the influence was methodological or involved adapted code.

## External dependencies and assets

Python dependencies retain their own licenses and are installed separately. FFmpeg, fonts, ASR models, music, LUTs, and user media are not bundled. Obtain these separately under their applicable licenses. A music-license label entered into a configuration is metadata, not evidence of usage or redistribution rights.

# Audio Forensic source

Upstream: https://github.com/spideyonmoon/audio-forensic

These files were copied from the previously tested Orion checkout.

Local checkout identifier:

`b10cab6f29e9a43677e3adae97a7c39e0793268`

The upstream repository no longer exposes that commit, so the exact tested
files are vendored here to keep container builds reproducible.

## SonicSentry integration patch (0.2.0)

An optional `--spectrogram-dir` CLI argument and matching function arguments
route generated spectrograms to SonicSentry's report storage. Names use a hash
of the input path to avoid collisions; stale derived images are removed before
rendering. Without the option the standalone engine retains its original
behavior. Forensic calculations, thresholds and verdicts are unchanged.

This keeps source albums read-only during analysis and allows the application
to display generated spectrograms from its managed report directory.

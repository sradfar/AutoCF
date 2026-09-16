# AutoCF 1.0.0 macOS Apple Silicon

Status: **native CI build verified and downloaded as a tester DMG**.

Artifact: `AutoCF-1.0.0-macOS-arm64.dmg` (1,309,527,084 bytes).

The macOS 15 ARM64 runner embedded native PySide6/QtWebEngine, verified its
helper/frameworks, signed the `.app`, created the DMG, and passed the integrity
gate. Local SHA-256 verification matches its CI sidecar:
`669f5b3ab4df4682b48ead4dc451ba87b7df0873a3cb3550a3d5318e0ef853cf`.

This is ad-hoc signed for testing. Normal Gatekeeper distribution still needs
the publisher's Apple Developer ID signing certificate and notarization.


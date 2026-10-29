# Maintainer: Mattia Melodia
pkgname=hda-pipewire-split-git
pkgver=1.0.0
pkgrel=1
pkgdesc="Universal Realtek HDA PipeWire dual-output splitter, isolated Both sink and per-tab router"
arch=('any')
url="https://github.com/mattiamelodia/alc285-pipewire-split"
license=('MIT')
depends=('python' 'python-pyqt6' 'pipewire' 'pipewire-pulse' 'wireplumber' 'alsa-utils')
optdepends=('playerctl: Enhanced MPRIS browser tab title detection')
provides=('hda-audio-control')

package() {
    install -d "$pkgdir/usr/lib/hda-pipewire-split"
    cp -r "$srcdir/hda_split" "$srcdir/main.py" "$srcdir/wizard.py" "$srcdir/icon.png" \
        "$pkgdir/usr/lib/hda-pipewire-split/"

    install -d "$pkgdir/usr/bin"
    cat << 'EOF' > "$pkgdir/usr/bin/hda-audio-control"
#!/usr/bin/env bash
exec python3 /usr/lib/hda-pipewire-split/main.py "$@"
EOF
    chmod 755 "$pkgdir/usr/bin/hda-audio-control"

    install -Dm644 "$srcdir/icon.png" "$pkgdir/usr/share/pixmaps/hda-pipewire-split.png"
    install -Dm644 "$srcdir/hda-audio-control.desktop" "$pkgdir/usr/share/applications/hda-audio-control.desktop"
}
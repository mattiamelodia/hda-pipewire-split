# Maintainer: Mattia Melodia
pkgname=hda-pipewire-split-git
_pkgname=hda-pipewire-split
pkgver=r2.fe01036
pkgrel=1
pkgdesc="Split a Realtek HDA card's pro-audio PipeWire node into Headphones, Speakers and Both sinks, with a per-application router"
arch=('any')
url="https://github.com/mattiamelodia/hda-pipewire-split"
license=('MIT')
depends=('python' 'python-pyqt6' 'pipewire' 'pipewire-pulse' 'wireplumber' 'alsa-utils' 'libnotify')
makedepends=('git')
optdepends=('playerctl: MPRIS media title lookup for application names')
provides=('hda-audio-control' 'hda-pipewire-split')
conflicts=('hda-pipewire-split')
source=("git+https://github.com/mattiamelodia/hda-pipewire-split.git")
sha256sums=('SKIP')

pkgver() {
    cd "$srcdir/$_pkgname"
    printf "r%s.%s" "$(git rev-list --count HEAD)" "$(git rev-parse --short HEAD)"
}

package() {
    cd "$srcdir/$_pkgname"

    local libdir="$pkgdir/usr/lib/hda-pipewire-split"

    # Python package (sources only, no __pycache__)
    find hda_split -type f -name '*.py' -exec install -Dm644 {} "$libdir/{}" \;
    install -Dm755 main.py "$libdir/main.py"
    install -Dm755 wizard.py "$libdir/wizard.py"
    # config.py falls back to <install dir>/icon.png for the notification and window icon
    install -Dm644 icon.png "$libdir/icon.png"

    install -d "$pkgdir/usr/bin"
    cat > "$pkgdir/usr/bin/hda-audio-control" << 'WRAPPER'
#!/usr/bin/env bash
exec python3 /usr/lib/hda-pipewire-split/main.py "$@"
WRAPPER
    chmod 755 "$pkgdir/usr/bin/hda-audio-control"

    install -Dm644 icon.png "$pkgdir/usr/share/pixmaps/hda-pipewire-split.png"
    install -Dm644 hda-audio-control.desktop "$pkgdir/usr/share/applications/hda-audio-control.desktop"
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
}

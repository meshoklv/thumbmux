# Third-party notices

thumbmux itself bundles no third-party code or assets (Python standard library only; the web page is self-contained, no CDN).

Runtime dependencies installed separately by the user / Docker image:

- [tmux](https://github.com/tmux/tmux) — ISC license
- [ttyd](https://github.com/tsl0922/ttyd) — MIT license (serves the xterm.js terminal page)
- Optional (`docker-compose.vpn.yml`): [gluetun](https://github.com/qdm12/gluetun) — MIT license

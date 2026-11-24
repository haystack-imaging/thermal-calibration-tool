# Bundled resources

Everything in this folder goes into the installed app (`bundle.resources` in `tauri.conf.json`).

`demo-flight/` is the demo flight behind *Open → Open demo flight*. It is generated, not committed:

```bash
python demo/make_demo_flight.py rust/app/src-tauri/resources/demo-flight --every 4
```

The release workflow (`.github/workflows/release-app.yml`) makes it once and puts the same flight into
every installer and into `demo-flight.zip`. A build without it still works; the menu entry then says
where to download the demo.

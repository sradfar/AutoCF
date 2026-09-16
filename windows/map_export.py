"""High-resolution, framed export of the live MapLibre view (no graticule)."""
from __future__ import annotations

import json
import math
from pathlib import Path

from PySide6.QtCore import QDir, QRectF, Qt, QTimer, QUrl, QTemporaryFile, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import QWidget
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineSettings


SNAPSHOT_JS = """JSON.stringify({style:map.getStyle(),center:map.getCenter().toArray(),
zoom:map.getZoom(),bearing:map.getBearing(),pitch:map.getPitch(),
width:map.getContainer().clientWidth,height:map.getContainer().clientHeight})"""


def export_html(snapshot: dict) -> str:
    # Escape script terminators in feature names and provider attribution.
    data = json.dumps(snapshot).replace("<", "\\u003c")
    return """<!doctype html><html><head><meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
<style>html,body,#map{margin:0;width:100%;height:100%;overflow:hidden}</style>
</head><body><div id="map"></div>
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script><script>
window.exportReady=false;window.exportErrors=[];
const s=SNAPSHOT;
const map=new maplibregl.Map({container:'map',style:s.style,center:s.center,
zoom:s.zoom,bearing:s.bearing,pitch:s.pitch,interactive:false,
preserveDrawingBuffer:true,attributionControl:false,fadeDuration:0});
map.addControl(new maplibregl.NavigationControl());
map.addControl(new maplibregl.ScaleControl({unit:'metric'}));
map.addControl(new maplibregl.AttributionControl({compact:false}));
map.on('error',e=>window.exportErrors.push(String(e.error||e)));
map.on('idle',()=>{window.exportReady=true;});
window.exportState=()=>{
 const w=map.getContainer().clientWidth,h=map.getContainer().clientHeight;
 const ticks={top:[],bottom:[],left:[],right:[]};
 for(let i=0;i<=4;i++){
  const f=0.1+i*0.2;
  ticks.top.push([f,map.unproject([f*w,0]).lng]);
  ticks.bottom.push([f,map.unproject([f*w,h]).lng]);
  ticks.left.push([f,map.unproject([0,f*h]).lat]);
  ticks.right.push([f,map.unproject([w,f*h]).lat]);
 }
 return JSON.stringify({ready:window.exportReady&&map.areTilesLoaded(),
 errors:window.exportErrors,ticks:ticks,width:w,height:h});
};
</script></body></html>""".replace("SNAPSHOT", data)


def coordinate_label(value: float, longitude: bool) -> str:
    if longitude:
        value = (value + 180.0) % 360.0 - 180.0
    suffix = ("E" if value >= 0 else "W") if longitude else ("N" if value >= 0 else "S")
    return f"{abs(value):.4f}°{suffix}"


def frame_image(image: QImage, ticks: dict, dpi: int = 300) -> QImage:
    """Ticks follow each actual projected edge, including rotated map views."""
    margin = round(dpi * 0.42)
    out = QImage(image.width() + 2 * margin, image.height() + 2 * margin,
                 QImage.Format.Format_ARGB32)
    out.fill(QColor("white"))
    out.setDotsPerMeterX(round(dpi / 0.0254))
    out.setDotsPerMeterY(round(dpi / 0.0254))
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.drawImage(margin, margin, image)
    p.setPen(QPen(QColor("black"), max(1, dpi / 150)))
    p.drawRect(QRectF(margin, margin, image.width(), image.height()))
    font = QFont("Arial"); font.setPixelSize(round(dpi * 8 / 72)); p.setFont(font)
    tick = dpi * 0.025
    for side, pairs in ticks.items():
        for fraction, value in pairs:
            if not math.isfinite(value):
                continue
            horizontal = side in {"top", "bottom"}
            label = coordinate_label(value, horizontal)
            if horizontal:
                x = margin + fraction * image.width()
                y = margin if side == "top" else margin + image.height()
                sign = -1 if side == "top" else 1
                p.drawLine(int(x), int(y), int(x), int(y + sign * tick))
                rect = QRectF(x - margin, y - margin * .65 if side == "top" else y + tick,
                              2 * margin, margin * .5)
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, label)
            else:
                x = margin if side == "left" else margin + image.width()
                y = margin + fraction * image.height()
                sign = -1 if side == "left" else 1
                p.drawLine(int(x), int(y), int(x + sign * tick), int(y))
                p.save(); p.translate(x + sign * margin * .48, y); p.rotate(-90)
                p.drawText(QRectF(-margin, -margin * .25, 2 * margin, margin * .5),
                           Qt.AlignmentFlag.AlignCenter, label)
                p.restore()
    p.end()
    return out


class MapPngExport(QWidget):
    finished = Signal(str, str)

    def __init__(self, snapshot: dict, width_inches: float, path: Path, overlays: list):
        super().__init__()
        self.path, self.overlays, self.snapshot = path, overlays, snapshot
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # Width includes the frame. Render the browser at actual output pixels.
        width = round(width_inches * 300) - 2 * round(300 * .42)
        self.scale = width / snapshot["width"]
        height = round(snapshot["height"] * self.scale)
        self.resize(width, height)
        self.web = QWebEngineView(self)
        self.web.resize(width, height)
        self.web.setZoomFactor(self.scale)
        self.ticks = 0
        self.timer = QTimer(self); self.timer.setInterval(250)
        self.timer.timeout.connect(self.poll)
        # Avoid setHtml's data-URL size limit for maps with raster overlays or
        # large AOI geometries. Qt owns and removes this temporary HTML file.
        self.page_file = QTemporaryFile(QDir.tempPath() + "/autocf-map-XXXXXX.html", self)
        if not self.page_file.open():
            raise OSError(self.page_file.errorString())
        self.page_file.write(export_html(snapshot).encode("utf-8")); self.page_file.flush()
        self.web.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.web.setUrl(QUrl.fromLocalFile(self.page_file.fileName()))
        self.show()
        self.timer.start()

    def poll(self):
        self.ticks += 1
        if self.ticks > 180:
            self.finish("Map export timed out. Check the basemap connection and try again.")
            return
        self.web.page().runJavaScript(
            "typeof exportState==='function'?exportState():''", self.receive)

    def receive(self, payload):
        if not self.timer.isActive() or not payload:
            return
        state = json.loads(payload)
        if state["errors"]:
            self.finish("Map tiles/layers failed to load: " + "; ".join(state["errors"][:3]))
        elif state["ready"]:
            self.timer.stop()
            # Allow the compositor to present its completed WebGL frame.
            QTimer.singleShot(350, lambda: self.capture(state))

    def capture(self, state):
        try:
            image = self.web.grab().toImage()
            if image.width() != self.web.width() or image.height() != self.web.height():
                image = image.scaled(self.web.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation)
            image.setDevicePixelRatio(1)
            if image.isNull():
                raise ValueError("The browser returned an empty map image.")
            sx, sy = image.width() / self.snapshot["width"], image.height() / self.snapshot["height"]
            painter = QPainter(image)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for overlay, x, y in self.overlays:
                painter.drawImage(QRectF(x * sx, y * sy, overlay.width(), overlay.height()), overlay)
            painter.end()
            result = frame_image(image, state["ticks"])
            # QSaveFile prevents a failed export from truncating an existing PNG.
            from PySide6.QtCore import QSaveFile, QIODevice
            output = QSaveFile(str(self.path))
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            if not result.save(output, "PNG"):
                output.cancelWriting(); raise OSError("PNG encoding failed.")
            if not output.commit():
                raise OSError(output.errorString())
            self.finish("")
        except Exception as exc:
            self.finish(str(exc))

    def finish(self, error):
        self.timer.stop()
        self.finished.emit(str(self.path), error)
        self.close()

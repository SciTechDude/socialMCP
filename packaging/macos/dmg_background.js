#!/usr/bin/osascript -l JavaScript
//
// Draws the background image for the disk-image window.
//
//   osascript -l JavaScript dmg_background.js <output.png> [version]
//
// Why draw it rather than commit a PNG: this repository is text, and a
// reviewer can read what the install window will say. It also means the
// version can appear on the image without an asset edit per release.
//
// Why the image matters at all: dragging an app to Applications is a file
// copy, so macOS has nothing to announce and shows no progress and no "done".
// A first-time user drags the icon and sees nothing happen. The arrow and the
// two labelled drop points are the only instruction they get, which is why
// every well-packaged Mac app has them and why a bare file listing — what
// this dmg used to be — reads as "the download did nothing".
//
// Deliberately no external dependency: AppKit draws it through the
// Objective-C bridge, the same bridge the first-run dialogs already use.

ObjC.import('AppKit');

function run(argv) {
    const out = argv[0];
    if (!out) {
        throw new Error('usage: dmg_background.js <output.png> [version]');
    }
    const version = argv[1] || '';

    // Matches the window bounds set by the layout script. Points, not pixels;
    // the @2x sharpness comes from drawing into a 2x bitmap below.
    const W = 600;
    const H = 400;
    const SCALE = 2;

    const bitmap = $.NSBitmapImageRep.alloc
        .initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
            $(), W * SCALE, H * SCALE, 8, 4, true, false,
            $.NSCalibratedRGBColorSpace, 0, 0);
    bitmap.setSize($.NSMakeSize(W, H));

    const context = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(bitmap);
    $.NSGraphicsContext.saveGraphicsState;
    // setCurrentContext(), not an assignment to `currentContext`: through the
    // bridge the assignment silently does nothing, every drawing call then
    // goes to no context at all, and the result is a blank image with no
    // error anywhere.
    $.NSGraphicsContext.setCurrentContext(context);

    // A very light neutral wash. Anything stronger competes with the icons,
    // and a white background is indistinguishable from an empty window.
    $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.97, 0.97, 0.98, 1.0).set;
    $.NSBezierPath.fillRect($.NSMakeRect(0, 0, W, H));

    // The arrow, pointing from the app icon to the Applications folder. Drawn
    // between the two icon positions the layout script uses (x=150 and x=450),
    // at the icons' vertical centre.
    const arrowY = H - 170;
    const stroke = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.55, 0.57, 0.62, 1.0);
    stroke.set;
    const shaft = $.NSBezierPath.bezierPath;
    shaft.setLineWidth(6);
    shaft.setLineCapStyle($.NSLineCapStyleRound);
    shaft.moveToPoint($.NSMakePoint(252, arrowY));
    shaft.lineToPoint($.NSMakePoint(338, arrowY));
    shaft.stroke;
    const head = $.NSBezierPath.bezierPath;
    head.moveToPoint($.NSMakePoint(368, arrowY));
    head.lineToPoint($.NSMakePoint(334, arrowY + 17));
    head.lineToPoint($.NSMakePoint(334, arrowY - 17));
    head.closePath;
    head.fill;

    drawCentred('Drag SocialMCP onto Applications', 22, 0.18, H - 268, W);
    drawCentred('Then open it from Applications — it will not open from here.',
                13, 0.42, H - 296, W);
    if (version) {
        drawCentred(version, 11, 0.62, 26, W);
    }

    $.NSGraphicsContext.restoreGraphicsState;

    const png = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
    if (!png.writeToFileAtomically($(out), true)) {
        throw new Error('could not write ' + out);
    }
    return out;
}

// Text is drawn through NSString's own drawing category, not an attributed
// string: JXA exposes no NSAttributedString initialiser at all (the same
// limitation that forced the terms sheet to be built as RTF). The category
// methods on a real NSString object are bridged, so this route works.
function drawCentred(text, size, grey, y, width) {
    const colour = $.NSColor.colorWithCalibratedRedGreenBlueAlpha(grey, grey, grey + 0.04, 1.0);
    // Built key by key. The variadic `dictionaryWithObjectsForKeys` pairs the
    // two arrays wrongly through the bridge, which shows up as AppKit asking
    // a dictionary for `lineBreakMode` — a confusing error a long way from
    // its cause.
    const attributes = $.NSMutableDictionary.dictionary;
    attributes.setObjectForKey($.NSFont.systemFontOfSizeWeight(size, size > 18 ? 0.3 : 0.0), 'NSFont');
    attributes.setObjectForKey(colour, 'NSColor');

    // Centred by measuring, not by an NSParagraphStyle: a centring paragraph
    // style put the text well right of centre and clipped it off the edge.
    // `sizeWithAttributes` is exact for the font actually used, so this also
    // survives a wording change without anyone re-checking the layout.
    const string = $.NSString.stringWithUTF8String(text);
    const measured = string.sizeWithAttributes(attributes);
    const x = (width - measured.width) / 2;
    string.drawAtPointWithAttributes($.NSMakePoint(x, y), attributes);
}

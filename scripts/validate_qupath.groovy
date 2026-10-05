// QuPath 0.6.x headless validation. Read-only; no save/project modification.
// qupath script -i IMAGE -a RESULT.json -a EXPECTED.json validate_qupath.groovy
// EXPECTED: X/Y/C/Z/T (or SizeX...), rgb, dtype, channels (names or {name}),
// physical_size_x/y in micrometers; optional roi_checks[{c,x,y,w,h,sha256}].
import com.google.gson.Gson
import com.google.gson.GsonBuilder
import qupath.lib.regions.RegionRequest
import qupath.lib.common.GeneralTools
import qupath.lib.images.servers.ImageServerMetadata.ImageResolutionLevel
import javax.imageio.ImageIO
import java.awt.image.BufferedImage
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.security.MessageDigest

def gson = new GsonBuilder().setPrettyPrinting().create()
if (args.length < 2) throw new IllegalArgumentException('Expected output JSON and expected metadata JSON arguments')
def output = new File(args[0])
output.parentFile?.mkdirs()
def expected = new Gson().fromJson(new File(args[1]).getText('UTF-8'), Map)
def result = [status:'RUNNING', validation:'actual_qupath_headless_read', gui_opened:false,
              qupath_version:GeneralTools.getVersion(), checked_at_utc:java.time.Instant.now().toString(), checks:[:], warnings:[],reader_limitations:[]]
def number = { String key -> expected.containsKey(key) ? expected[key] : expected['Size'+key] }
def failUnless = { boolean condition, String message -> if (!condition) throw new IllegalStateException(message) }
try {
    def server = getCurrentServer()
    failUnless(server != null, 'QuPath did not open an image server')
    def meta = server.getMetadata()
    def channels = meta.getChannels().collect { it.getName() }
    def dtype = server.getPixelType().toString().toLowerCase()
    result.server_class = server.getClass().getName()
    result.metadata = [X:server.getWidth(),Y:server.getHeight(),C:server.nChannels(),Z:server.nZSlices(),T:server.nTimepoints(),
                       rgb:server.isRGB(),dtype:dtype,channels:channels,
                       physical_size_x:server.getPixelCalibration().getPixelWidthMicrons(),
                       physical_size_y:server.getPixelCalibration().getPixelHeightMicrons()]
    // Gson refuses non-finite numbers: missing calibration is an explicit null.
    ['physical_size_x','physical_size_y'].each { key ->
        if (!Double.isFinite(result.metadata[key] as double)) result.metadata[key] = null
    }
    ['X','Y','C','Z','T'].each { key ->
        def value = number(key)
        if (value != null) failUnless((result.metadata[key] as int)==(value as int), key+' differs from source metadata')
    }
    if (expected.containsKey('rgb')) failUnless(server.isRGB()==expected.rgb, 'RGB/independent-channel mismatch')
    if (expected.dtype) failUnless(dtype==expected.dtype.toString().toLowerCase(), 'Pixel type differs from source metadata')
    if (expected.channels && !server.isRGB()) {
        def names = expected.channels.collect { it instanceof Map ? (it.name ?: it.Name) : it }
        names.eachWithIndex { name, c -> if (name != null) failUnless(c < channels.size() && channels[c]==name, 'Channel name/order mismatch at '+c) }
    }
    ['physical_size_x','physical_size_y'].each { key ->
        if (expected[key] instanceof Number) {
            failUnless(result.metadata[key] != null, 'Pixel calibration missing: '+key)
            double actual = result.metadata[key] as double
            double wanted = expected[key] as double
            failUnless(Math.abs(actual-wanted)<=Math.max(1e-6,Math.abs(wanted)*1e-6), 'Pixel calibration changed: '+key)
        }
    }
    result.checks.metadata = 'PASS'
    int width = server.getWidth(), height = server.getHeight()
    def checkBands = { image ->
        failUnless(image != null, 'QuPath returned an empty region')
        int bands = image.getRaster().getNumBands()
        failUnless(server.isRGB() ? bands>=3 : bands==server.nChannels(), 'Not all channels were returned')
        return bands
    }
    def locations = [[0,0],[(width-1).intdiv(2),(height-1).intdiv(2)],[Math.max(0,width-128),Math.max(0,height-128)]]
    result.full_resolution_reads = locations.collect { xy ->
        int x=xy[0], y=xy[1], w=Math.min(128,width-x), h=Math.min(128,height-y)
        def img=server.readRegion(RegionRequest.createInstance(server.getPath(),1.0,x,y,w,h,0,0))
        [x:x,y:y,w:w,h:h,bands:checkBands(img)]
    }
    result.checks.full_resolution_all_channels = 'PASS'
    def downsamples=server.getPreferredDownsamples()
    def actualLevels=meta.getLevels().collect { [width:it.getWidth(),height:it.getHeight(),downsample:it.getDownsample()] }
    result.pyramid_structure=[actual_levels:actualLevels,expected_count:expected.expected_pyramid_levels]
    boolean tinyTailLimit=false
    if (expected.pyramid_dimensions) {
        def wanted=expected.pyramid_dimensions.collect { [width:it.width as int,height:it.height as int] }
        def actual=actualLevels.collect { [width:it.width,height:it.height] }
        result.pyramid_structure.expected_dimensions=wanted
        failUnless(wanted.size()==(expected.expected_pyramid_levels as int), 'Expected pyramid count and dimensions disagree')
        failUnless(actual.size()<=wanted.size() && actual==wanted.take(actual.size()), 'QuPath pyramid raster dimensions differ from the verified output')
        if (actual.size()!=wanted.size()) {
            // QuPath 0.6.0 can round the native 2x1 and 1x1 terminal rasters to
            // one downsample. Only this exact, independently reproducible tail
            // limitation is permitted; all other missing levels remain errors.
            def readerClass=server.getClass().getName()
            failUnless(expected.kfb && GeneralTools.getVersion()=='0.6.0' &&
                (readerClass in ['qupath.lib.images.servers.bioformats.BioFormatsImageServer','qupath.lib.images.servers.openslide.OpenslideImageServer']) &&
                wanted.size()==actual.size()+1 && wanted.last()==[width:1,height:1] &&
                (actual.last() in [[width:2,height:1],[width:1,height:2]]),
                'QuPath omitted a pyramid level outside the documented 0.6.0 terminal-1x1 limitation')
            def builder=new ImageResolutionLevel.Builder(width,height)
            wanted.each { builder.addLevel(it.width as int,it.height as int) }
            def predicted=builder.build().collect { [width:it.getWidth(),height:it.getHeight(),downsample:it.getDownsample()] }
            failUnless(predicted==actualLevels, 'QuPath level omission does not match its documented downsample rounding')
            tinyTailLimit=true
            result.pyramid_structure.unavailable_in_qupath=[wanted.last()]
            result.pyramid_structure.limitation='QuPath 0.6.0 downsample rounding merges terminal 2x1/1x2 and 1x1 rasters; all output levels remain present and separately pixel-validated.'
            result.warnings.add(result.pyramid_structure.limitation)
            result.reader_limitations.add([code:'QUPATH_060_TERMINAL_1X1',exported_levels:wanted.size(),readable_levels:actual.size(),unavailable_dimensions:[wanted.last()]])
        }
    }
    if (expected.expected_pyramid_levels != null) failUnless(tinyTailLimit || downsamples.length == (expected.expected_pyramid_levels as int), 'QuPath reports fewer or different pyramid levels than the output structure')
    result.pyramid_reads=downsamples.collect { ds ->
        int w=Math.min(width,Math.max(1,(128*ds) as int)), h=Math.min(height,Math.max(1,(128*ds) as int))
        int x=Math.max(0,(width-w).intdiv(2)), y=Math.max(0,(height-h).intdiv(2))
        def img=server.readRegion(RegionRequest.createInstance(server.getPath(),ds as double,x,y,w,h,0,0))
        [downsample:ds,width:img.getWidth(),height:img.getHeight(),bands:checkBands(img)]
    }
    result.checks.all_pyramid_levels = tinyTailLimit ? 'PASS_WITH_READER_LIMITATION' : 'PASS'
    result.checks.available_pyramid_levels = 'PASS'
    result.checks.all_exported_pyramid_levels = tinyTailLimit ? 'NOT_ALL_EXPOSED' : 'PASS'
    result.roi_checks=[]
    (expected.roi_checks ?: []).each { roi ->
        int x=roi.x as int,y=roi.y as int,w=roi.w as int,h=roi.h as int,c=roi.c as int
        failUnless(c>=0 && c<server.nChannels() && x>=0 && y>=0 && w>0 && h>0 && x+w<=width && y+h<=height,'ROI outside image')
        def img=server.readRegion(RegionRequest.createInstance(server.getPath(),1.0,x,y,w,h,0,0))
        checkBands(img)
        int bytesPerSample=['uint8':1,'uint16':2,'int16':2,'float32':4][dtype] ?: 0
        failUnless(bytesPerSample>0,'ROI checksum unsupported for '+dtype)
        def buffer=ByteBuffer.allocate(w*h*bytesPerSample).order(ByteOrder.LITTLE_ENDIAN)
        for(int yy=0;yy<h;yy++) for(int xx=0;xx<w;xx++) {
            double value
            if(server.isRGB()) {
                int rgb=img.getRGB(xx,yy)
                value=(rgb >> (16-8*c)) & 255
            } else value=img.getRaster().getSampleDouble(xx,yy,c)
            switch(dtype) {
                case 'uint8': buffer.put((byte)((int)value)); break
                case 'uint16': case 'int16': buffer.putShort((short)((int)value)); break
                case 'float32': buffer.putFloat((float)value); break
            }
        }
        String hash=MessageDigest.getInstance('SHA-256').digest(buffer.array()).encodeHex().toString()
        failUnless(hash==roi.sha256,'Source/QuPath ROI pixels differ at '+[c:c,x:x,y:y])
        result.roi_checks.add([c:c,x:x,y:y,w:w,h:h,sha256:hash,passed:true])
    }
    result.checks.source_roi_pixels = expected.roi_checks ? 'PASS' : 'NOT_REQUESTED'
    double ds=Math.max(1.0,Math.max(width,height)/1024.0)
    def overview=server.readRegion(RegionRequest.createInstance(server.getPath(),ds,0,0,width,height,0,0))
    checkBands(overview)
    def preview=overview
    if (!server.isRGB()) {
        // A display-only stretch of channel 0. This is not exported scientific data.
        def raster=overview.getRaster()
        double lo=Double.POSITIVE_INFINITY,hi=Double.NEGATIVE_INFINITY
        for(int y=0;y<overview.getHeight();y++) for(int x=0;x<overview.getWidth();x++) {
            double v=raster.getSampleDouble(x,y,0)
            if(Double.isFinite(v)) {lo=Math.min(lo,v);hi=Math.max(hi,v)}
        }
        failUnless(Double.isFinite(lo),'Non-finite overview')
        preview=new BufferedImage(overview.getWidth(),overview.getHeight(),BufferedImage.TYPE_BYTE_GRAY)
        for(int y=0;y<overview.getHeight();y++) for(int x=0;x<overview.getWidth();x++) {
            double v=raster.getSampleDouble(x,y,0)
            int out=hi>lo ? (int)Math.round(Math.max(0,Math.min(255,(v-lo)*255/(hi-lo)))) : 0
            preview.getRaster().setSample(x,y,0,out)
        }
        result.preview_display=[channel:0,min:lo,max:hi,normalization:'display_only_min_max']
        if(hi==0) result.warnings.add('Overview channel 0 is all zero; inspect representative source and output tissue regions')
    }
    def previewFile=new File(output.parentFile,output.name.replaceFirst(/\.json$/, '')+'.preview.png')
    failUnless(ImageIO.write(preview,'PNG',previewFile),'Could not export PNG preview')
    result.preview_file=previewFile.name
    result.checks.overview='PASS'
    result.status='PASS'
} catch(Throwable error) {
    result.status='FAIL'
    result.error=error.toString()
    output.setText(gson.toJson(result),'UTF-8')
    throw error
}
output.setText(gson.toJson(result),'UTF-8')
println('QUPATH_READ_VALIDATION_PASS')

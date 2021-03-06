# noqa
# coding=utf-8
from __future__ import absolute_import, division, print_function

import math

from haversine import haversine
import numpy as np
import rasterio
from rasterio import transform
from rasterio import windows
from rasterio.crs import CRS
from rasterio.transform import Affine
from rasterio.vrt import WarpedVRT
from rasterio.warp import Resampling
from rasterio.windows import Window
from scipy.interpolate import RectBivariateSpline

from . import mosaic

WEB_MERCATOR_CRS = CRS.from_epsg(3857)
WGS84_CRS = CRS.from_epsg(4326)

EXTENTS = {
    str(WEB_MERCATOR_CRS): (-20037508.342789244, -20037508.342789244,
                            20037508.342789244, 20037508.342789244),
    str(WGS84_CRS): (-180, -90, 180, 90),
}


def _isimage(data_format):
    return data_format.upper() in ["RGB", "RGBA"]


def _mask(data, nodata):
    if np.issubdtype(data.dtype, float):
        return np.ma.masked_values(data, nodata, copy=False)

    return np.ma.masked_equal(data, nodata, copy=False)


def _nodata(dtype):
    if np.issubdtype(dtype, int):
        return np.iinfo(dtype).min
    else:
        return np.finfo(dtype).min


def crop((data, (bounds, data_crs)), data_format, offsets):
    left,  right, bottom, top = offsets

    if _isimage(data_format):
        width, height, _ = data.shape

        data = data[top:height - bottom, left:width - right, :]

        return (data, (None, None))

    _, height, width = data.shape
    t = transform.from_bounds(*bounds, width=width, height=height)

    data = data[:, top:height - bottom, left:width - right]

    cropped_window = windows.Window(left, top, *data.shape[1:])
    cropped_bounds = windows.bounds(cropped_window, t)

    return (data, (cropped_bounds, data_crs))


def get_extent(crs):
    return EXTENTS[str(crs)]


def get_resolution((bounds, crs), (height, width)):
    t = transform.from_bounds(*bounds, width=width, height=height)

    return abs(t.a), abs(t.e)


def get_resolution_in_meters((bounds, crs), (height, width)):
    if crs.is_geographic:
        left = (bounds[0], (bounds[1] + bounds[3]) / 2)
        right = (bounds[2], (bounds[1] + bounds[3]) / 2)
        top = ((bounds[0] + bounds[2]) / 2, bounds[3])
        bottom = ((bounds[0] + bounds[2]) / 2, bounds[1])

        return (haversine(left, right) * 1000 / width,
                haversine(top, bottom) * 1000 / height)

    return get_resolution((bounds, crs), (height, width))


def get_source(path):
    """Cached source opening."""
    with rasterio.Env(CPL_VSIL_CURL_ALLOWED_EXTENSIONS='.vrt,.tif,.ovr,.msk'):
        return rasterio.open(path)


def get_zoom(resolution, op=round):
    return int(op(math.log((2 * math.pi * 6378137) /
                           (resolution * 256)) / math.log(2)))


def read_window(src, (bounds, bounds_crs), (height, width)):
    try:
        extent = get_extent(bounds_crs)
    except KeyError:
        raise Exception("Unsupported CRS: {}".format(bounds_crs))

    if bounds_crs == WEB_MERCATOR_CRS:
        # special case for web mercator; use a target image size that most
        # closely matches the source resolution (and is a power of 2)
        zoom = get_zoom(max(get_resolution_in_meters(
            (src.bounds, src.crs), (src.height, src.width))), op=math.ceil)

        dst_width = dst_height = (2 ** zoom) * 256
        resolution = ((extent[2] - extent[0]) / dst_width,
                      (extent[3] - extent[1]) / dst_height)

        dst_transform = Affine(resolution[0], 0.0, extent[0],
                               0.0, -resolution[1], extent[3])
    else:
        # use a target image size that most closely matches the target
        # resolution
        resolution = get_resolution((bounds, bounds_crs), (height, width))
        extent_width = extent[2] - extent[0]
        extent_height = extent[3] - extent[1]
        dst_width = (1 / resolution[0]) * extent_width
        dst_height = (1 / resolution[1]) * extent_height

        # ensure that we end up with a clean multiple of the target size (until
        # rasterio uses floating point window offsets)
        if width % 2 == 1 or height % 2 == 1:
            dst_width *= 2
            dst_height *= 2
            resolution = [res / 2 for res in resolution]

        dst_transform = Affine(resolution[0], 0.0, extent[0],
                               0.0, -resolution[1], extent[3])

    with WarpedVRT(
        src,
        src_nodata=src.nodata,
        dst_crs=bounds_crs,
        dst_width=dst_width,
        dst_height=dst_height,
        dst_transform=dst_transform,
        resampling=Resampling.lanczos,
    ) as vrt:
        dst_window = vrt.window(*bounds)

        scale_factor = (dst_window.num_cols / width,
                        dst_window.num_rows / height)

        if vrt.count == 1 and (scale_factor[0] < 1 or scale_factor[1] < 1):
            # buffer

            # number of "pixel-equivalents" to buffer
            buffer_pixels = 2

            # individual pixel size in scaled "pixels"
            pixel = (1 / scale_factor[0],
                     1 / scale_factor[1])

            # buffer sizing
            buffer = (buffer_pixels * pixel[0],
                      buffer_pixels * pixel[1])
            r, c = dst_window

            # expand the calculated window
            window = Window.from_ranges(
                (r[0] - buffer_pixels, r[1] + buffer_pixels),
                (c[0] - buffer_pixels, c[1] + buffer_pixels))

            if scale_factor[0] % 1 != 0 or scale_factor[1] % 1 != 0:
                scaled_transform = vrt.transform * Affine.scale(*scale_factor)

                # ideal window (in scaled space)
                target_window = windows.from_bounds(
                    *bounds, transform=scaled_transform, boundless=True)
                r, c = target_window

                # expand the calculated window
                # (this isn't a Window so we can keep floating point values)
                target_window = ((r[0] - buffer_pixels * scale_factor[1],
                                  r[1] + buffer_pixels * scale_factor[1]),
                                 (c[0] - buffer_pixels * scale_factor[0],
                                  c[1] + buffer_pixels * scale_factor[0]))

                # bounds actually covered by the calculated window (including
                # buffers)
                data_bounds = windows.bounds(window, vrt.transform)

                # data window (in scaled space)
                data_window = windows.from_bounds(
                    *data_bounds, transform=scaled_transform, boundless=True)

                tr, tc = target_window
                dr, dc = data_window

                minx = dr[0] - tr[0]
                miny = dc[0] - tc[0]
                maxx = width + (dr[1] - tr[1])
                maxy = height + (dc[1] - tc[1])

                # source data pixels mapped to target pixels
                x = np.linspace(minx - buffer[0],
                                maxx - buffer[0],
                                num=window.num_cols)
                y = np.linspace(miny - buffer[1],
                                maxy - buffer[1],
                                num=window.num_rows)
            else:
                x = np.linspace(-buffer[0],
                                (window.num_cols * pixel[0]) - buffer[0],
                                num=window.num_cols)
                y = np.linspace(-buffer[1],
                                (window.num_rows * pixel[1]) - buffer[1],
                                num=window.num_rows)

            data = vrt.read(
                1,
                window=window,
            )

            if vrt.nodata is not None:
                data = _mask(data, vrt.nodata)
            else:
                data = np.ma.masked_array(data, mask=False)

            interp = RectBivariateSpline(y, x, data)

            data = interp(
                np.arange(0, height),
                np.arange(0, width),
            )[np.newaxis]

            if vrt.nodata is not None:
                data = _mask(data, vrt.nodata)
            else:
                data = np.ma.masked_array(data, mask=False)
        else:
            data = vrt.read(
                out_shape=(vrt.count, height, width),
                window=dst_window,
            )

            if vrt.nodata is not None:
                data = _mask(data, vrt.nodata)
            else:
                data = np.ma.masked_array(data, mask=False)

        data = data.astype(np.float32)

    # open the mask separately so we can take advantage of its overviews
    try:
        with rasterio.open("{}.msk".format(src.name), crs=src.crs) as mask_src:
            with WarpedVRT(
                mask_src,
                src_crs=src.crs,
                src_transform=src.transform,
                dst_crs=bounds_crs,
                dst_width=dst_width,
                dst_height=dst_height,
                dst_transform=dst_transform,
            ) as mask_vrt:
                dst_window = vrt.window(*bounds)

                mask = mask_vrt.read(
                    out_shape=(vrt.count, height, width),
                    window=dst_window,
                )

                data.mask = ~mask
    except Exception:
        # no mask
        pass

    return (data, (bounds, bounds_crs))


# TODO does buffer actually belong here, vs. being the responsibility of the
# calling code?
def render(
    (bounds, bounds_crs),
    shape,
    target_crs,
    format,
    transformation=None,
    buffer=0
):
    """Render data intersecting bounds into shape using an optional
    transformation."""
    resolution = get_resolution((bounds, bounds_crs), shape)
    resolution_m = get_resolution_in_meters((bounds, bounds_crs), shape)

    effective_buffer = buffer
    offset = 0

    if transformation and hasattr(transformation, 'buffer'):
        effective_buffer = buffer + transformation.buffer
        offset = transformation.buffer

    # apply buffer
    bounds_orig = bounds
    shape = [dim + (2 * effective_buffer) for dim in shape]
    bounds = [p - (effective_buffer * resolution[i % 2]) if i < 2 else
              p + (effective_buffer * resolution[i % 2])
              for i, p in enumerate(bounds)]

    left = right = bottom = top = offset

    # adjust bounds + shape if bounds extends outside the extent
    extent = get_extent(bounds_crs)

    if bounds[0] < extent[0]:
        shape[1] -= effective_buffer
        bounds[0] = bounds_orig[0]
        left = 0

    if bounds[2] > extent[2]:
        shape[1] -= effective_buffer
        bounds[2] = bounds_orig[2]
        right = 0

    if bounds[1] < extent[1]:
        shape[0] -= effective_buffer
        bounds[1] = bounds_orig[1]
        bottom = 0

    if bounds[3] > extent[3]:
        shape[0] -= effective_buffer
        bounds[3] = bounds_orig[3]
        top = 0

    sources = mosaic.get_sources((bounds, bounds_crs), resolution_m)

    (data, (data_bounds, data_crs)) = mosaic.composite(
        sources, (bounds, bounds_crs), shape, target_crs)

    data_format = "raw"

    if transformation:
        (data, data_format) = transformation((data, (data_bounds, data_crs)))

    if effective_buffer > buffer:
        (data, (data_bounds, data_crs)) = crop(
            (data, (data_bounds, data_crs)),
            data_format,
            (left, right, bottom, top))

    return format((data, (data_bounds, data_crs)), data_format)

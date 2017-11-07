# Copyright 2017 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import re
import logging
import tempfile
from urlparse import urlparse
import shutil
import urllib2
import zipfile
import tarfile
import csv
import hashlib
import multiprocessing
import subprocess
from functools import partial
from xml.sax.saxutils import escape

from sqlalchemy.schema import Column
from sqlalchemy.types import Integer
import requests
import click
import fiona

import bcdata
import pgdb

# --------------------------------------------
# Change default database/paths/filenames etc here (see README.md)
# --------------------------------------------
CONFIG = {
    "source_data": "source_data",
    "source_csv": "sources.csv",
    "out_table": "designatedlands",
    "out_file": "designatedlands.gpkg",
    "out_format": "GPKG",
    "db_url":
    "postgresql://postgres:postgres@localhost:5432/designatedlands",
    "n_processes": multiprocessing.cpu_count() - 1
    }
# --------------------------------------------
# --------------------------------------------

CHUNK_SIZE = 1024

logging.basicConfig(level=logging.INFO)

HELP = {
    "csv": 'Path to csv that lists all input data sources',
    "email": 'A valid email address, used for DataBC downloads',
    "dl_path": 'Path to folder holding downloaded data',
    "alias": "The 'alias' key identifing the source of interest, from source csv",
    "out_file": "Output geopackage name",
    "out_format": "Output format. Default GPKG (Geopackage)",
    "out_table": 'Name of output designated lands table'}


def get_files(path):
    """Returns an iterable containing the full path of all files in the
    specified path.
    https://github.com/OpenBounds/Processing/blob/master/utils.py
    """
    if os.path.isdir(path):
        for (dirpath, dirnames, filenames) in os.walk(path):
            for filename in filenames:
                if not filename[0] == '.':
                    yield os.path.join(dirpath, filename)
    else:
        yield path


def read_csv(path):
    """
    Load input csv file and return a list of dicts.
    - List is sorted by 'hierarchy' column
    - keys/columns added:
        + 'input_table'   - 'a'+hierarchy+'_'+src_table
        + 'tiled_table'   - 'b'+hierarchy+'_'+src_table
        + 'cleaned_table' - 'c'+hierarchy+'_'+src_table
    """
    source_list = [source for source in csv.DictReader(open(path, 'rb'))]
    for source in source_list:
        # convert hierarchy value to integer
        source.update((k, int(v)) for k, v in source.iteritems()
                      if k == "hierarchy" and v != '')
        # for convenience, add the layer names to the dict
        hierarchy = str(source["hierarchy"]).zfill(2)
        input_table = "a"+hierarchy+"_"+source["alias"]
        tiled_table = "b"+hierarchy+"_"+source["alias"]
        cleaned_table = "c"+hierarchy+"_"+source["alias"]
        source.update({"input_table": input_table,
                       "cleaned_table": cleaned_table,
                       "tiled_table": tiled_table})
    # return sorted list https://stackoverflow.com/questions/72899/
    return sorted(source_list, key=lambda k: k['hierarchy'])


def make_sure_path_exists(path):
    """
    Make directories in path if they do not exist.
    Modified from http://stackoverflow.com/a/5032238/1377021
    """
    try:
        os.makedirs(path)
        return path
    except:
        pass


def get_path_parts(path):
    """Splits a path into parent directories and file.
    """
    return path.split(os.sep)


def download_bcgw(url, dl_path, email=None, gdb=None):
    """Download BCGW data using DWDS
    """
    # make sure an email is provided
    if not email:
        email = os.environ["BCDATA_EMAIL"]
    if not email:
        raise Exception("An email address is required to download BCGW data")
    download = bcdata.download(url, email)
    if not download:
        raise Exception("Failed to create DWDS order")
    # move the download to specified dl_path, deleting if it already exists
    out_gdb = os.path.split(download)[1]
    if os.path.exists(os.path.join(dl_path, out_gdb)):
        shutil.rmtree(os.path.join(dl_path, out_gdb))
    shutil.copytree(download, os.path.join(dl_path, out_gdb))
    return os.path.join(dl_path, out_gdb)


def download_non_bcgw(url, download_cache=None):
    """
    Download a file to location specified
    Modified from https://github.com/OpenBounds/Processing/blob/master/utils.py
    """

    parsed_url = urlparse(url)

    urlfile = parsed_url.path.split('/')[-1]
    _, extension = os.path.split(urlfile)

    fp = tempfile.NamedTemporaryFile('wb', suffix=extension, delete=False)

    cache_path = None
    if download_cache is not None:
        cache_path = os.path.join(download_cache,
            hashlib.sha224(url).hexdigest())
        if os.path.exists(cache_path):
            info("Returning %s from local cache at %s" % (url, cache_path))
            fp.close()
            shutil.copy(cache_path, fp.name)
            return fp

    info('Downloading', url)
    if parsed_url.scheme == "http" or parsed_url.scheme == "https":
        res = requests.get(url, stream=True, verify=False)

        if not res.ok:
            raise IOError

        for chunk in res.iter_content(CHUNK_SIZE):
            fp.write(chunk)
    elif parsed_url.scheme == "ftp":
        download = urllib2.urlopen(url)

        file_size_dl = 0
        block_sz = 8192
        while True:
            buffer = download.read(block_sz)
            if not buffer:
                break

            file_size_dl += len(buffer)
            fp.write(buffer)

    fp.close()

    if cache_path:
        if not os.path.exists(download_cache):
            os.makedirs(download_cache)
        shutil.copy(fp.name, cache_path)

    return fp


def extract(fp, dl_path, alias, source_filename):
    """
    Unzip the archive, return path to specified file
    (this presumes that we already know the name of the desired file)
    Modified from https://github.com/OpenBounds/Processing/blob/master/utils.py
    """
    info('Extracting', fp.name)
    unzip_dir = make_sure_path_exists(os.path.join(dl_path, alias))
    info(unzip_dir)
    zipped_file = get_compressed_file_wrapper(fp.name)
    zipped_file.extractall(unzip_dir)
    zipped_file.close()
    return os.path.join(unzip_dir, source_filename)


def info(*strings):
    logging.info(' '.join(strings))


def error(*strings):
    logging.error(' '.join(strings))


class ZipCompatibleTarFile(tarfile.TarFile):
    """
    Wrapper around TarFile to make it more compatible with ZipFile
    Modified from https://github.com/OpenBounds/Processing/blob/master/utils.py
    """
    def infolist(self):
        members = self.getmembers()
        for m in members:
            m.filename = m.name
        return members

    def namelist(self):
        return self.getnames()


def get_compressed_file_wrapper(path):
    """ From https://github.com/OpenBounds/Processing/blob/master/utils.py
    """
    ARCHIVE_FORMAT_ZIP = "zip"
    ARCHIVE_FORMAT_TAR_GZ = "tar.gz"
    ARCHIVE_FORMAT_TAR_BZ2 = "tar.bz2"

    archive_format = None

    if path.endswith(".zip"):
        archive_format = ARCHIVE_FORMAT_ZIP
    elif path.endswith(".tar.gz") or path.endswith(".tgz"):
        archive_format = ARCHIVE_FORMAT_TAR_GZ
    elif path.endswith(".tar.bz2"):
        archive_format = ARCHIVE_FORMAT_TAR_BZ2
    else:
        try:
            with zipfile.ZipFile(path, "r") as f:
                archive_format = ARCHIVE_FORMAT_ZIP
        except:
            try:
                f = tarfile.TarFile.open(path, "r")
                f.close()
                archive_format = ARCHIVE_FORMAT_ZIP
            except:
                pass

    if archive_format is None:
        raise Exception("Unable to determine archive format")

    if archive_format == ARCHIVE_FORMAT_ZIP:
        return zipfile.ZipFile(path, 'r')
    elif archive_format == ARCHIVE_FORMAT_TAR_GZ:
        return ZipCompatibleTarFile.open(path, 'r:gz')
    elif archive_format == ARCHIVE_FORMAT_TAR_BZ2:
        return ZipCompatibleTarFile.open(path, 'r:bz2')


def get_tiles(db, table, tile_table="a00_tiles_250k"):
    """Return a list of all intersecting tiles from specified layer
    """
    sql = """SELECT DISTINCT b.map_tile
             FROM {table} a
             INNER JOIN {tile_table} b ON st_intersects(b.geom, a.geom)
             ORDER BY map_tile
          """.format(table=table,
                     tile_table=tile_table)
    return [r[0] for r in db.query(sql)]


def parallel_tiled(sql, tile, n_subs=2):
    """Create a connection and execute query for specified tile
       n_subs is the number of places in the sql query that should be substituted by the tile name
    """
    db = pgdb.connect(CONFIG["db_url"], schema="public", multiprocessing=True)
    db.execute(sql, (tile+"%",) * n_subs)


def clip(db, in_table, clip_table, out_table):
    """
    Clip geometry of in_table by clip_table, writing output to out_table
    """
    columns = ["a."+c for c in db[in_table].columns if c != 'geom']
    db[out_table].drop()
    sql = """CREATE UNLOGGED TABLE {temp} AS
             SELECT
               {columns},
               CASE
                 WHEN ST_CoveredBy(a.geom, b.geom) THEN a.geom
                 ELSE ST_Multi(
                        ST_CollectionExtract(
                          ST_Intersection(a.geom,b.geom), 3)) END AS geom
             FROM {in_table} AS a
             INNER JOIN {clip_table} AS b
             ON ST_Intersects(a.geom, b.geom)
          """.format(temp=out_table,
                     columns=", ".join(columns),
                     in_table=in_table,
                     clip_table=clip_table)
    info('Clipping %s by %s to create %s' % (in_table, clip_table, out_table))
    db.execute(sql)


def tile_sources(db, source_csv, alias=None, force=False):
    """
    - merge/union data within sources
    - cut sources by tile
    - repair source geom
    - add required columns
    """
    sources = read_csv(source_csv)
    # process only the source layer specified
    if alias:
        sources = [s for s in sources if s['alias'] == alias]
    # for all designated lands sources:
    # - create new table name prefixed with b_<hierarchy>
    # - create and populate standard columns:
    #     - designation (equivalent to source's alias in sources.csv)
    #     - designation_id (unique id of source feature)
    #     - designation_name (name of source feature)
    tile_sources = [s for s in sources
                     if s["exclude"] != 'T' and s['hierarchy'] != 0]
    for source in tile_sources:
        if source["tiled_table"] not in db.tables or force:
            info("Tiling and validating: %s" % source["alias"])
            db[source["tiled_table"]].drop()
            lookup = {"out_table": source["tiled_table"],
                      "src_table": source["input_table"],
                      "designation_id_col": source["designation_id_col"],
                      "designation_name_col": source["designation_name_col"]}
            sql = db.build_query(db.queries["prep1_merge_tile_a"], lookup)
            db.execute(sql)


def clean_and_agg_sources(db, source_csv, alias=None, force=False):
    """
    After sources are tiled and preprocessed, aggregation and cleaning is
    helpful to reduce topology exceptions in further processing. This is
    separate from the tiling / preprocessing because non-aggregated outputs
    (with the source designation name and id) are required.
    """
    sources = read_csv(source_csv)
    # process only the source layer specified
    if alias:
        sources = [s for s in sources if s['alias'] == alias]
    # for all designated lands sources:
    # - create new table name prefixed with c_<hierarchy>
    # - aggregate by designation, tile
    clean_sources = [s for s in sources
                     if s["exclude"] != 'T' and s['hierarchy'] != 0]
    for source in clean_sources:
        if source["cleaned_table"] not in db.tables or force:
            info("Cleaning and aggregating: %s" % source["alias"])
            db[source["cleaned_table"]].drop()
            lookup = {"out_table": source["cleaned_table"],
                      "src_table": source["tiled_table"]}
            sql = db.build_query(db.queries["prep2_clean_agg"], lookup)
            db.execute(sql)


def preprocess(db, source_csv, alias=None, force=False):
    """ Preprocess (eg clip) sources as specified in source_csv
    """
    sources = read_csv(source_csv)

    # process only the source layer specified
    if alias:
        sources = [s for s in sources if s['alias'] == alias]

    # apply pre-processing operation specified in sources.csv (eg clip)
    preprocess_sources = [s for s in sources
                          if s["preprocess_operation"] != '']
    for source in preprocess_sources:
        if source["input_table"]+"_preprc" not in db.tables or force:
            info("Preprocessing: %s" % source["alias"])
            # find name of the pre-process layer to be used
            preprocess_lyr = [s for s in sources
                              if s["alias"] == source["preprocess_layer_alias"]][0]
            # find the one character prefix used to designate input layers
            # (preprocessing only works with raw inputs, nothing tiled or
            # cleaned)
            input_prefix = source["input_table"][0]
            function = source["preprocess_operation"]
            # call the specified preprocess function
            info(source["input_table"])
            globals()[function](db,
                                source["input_table"],
                                input_prefix+"00_"+preprocess_lyr["alias"],
                                source["input_table"]+"_preprc")
            # overwrite the tiled table with the preprocessed table, but
            # retain the _preprc table as a flag that the job is done
            db[source["input_table"]].drop()
            db.execute("""CREATE TABLE {t} AS
                          SELECT * FROM {temp}""".format(t=source["input_table"],
                                                         temp=source["input_table"]+"_preprc"))
            # re-create spatial index
            db[source["input_table"]].create_index_geom()


def create_bc_boundary(db, n_processes):
    """
    Create a comprehensive land-marine layer by combining three sources.

    Note that specificly named source layers are hard coded and must exist:
    - bc_boundary_land (BC boundary layer from GeoBC, does not include marine)
    - bc_abms (BC Boundary, ABMS)
    - marine_ecosections (BC Marine Ecosections)
    """
    # create land/marine definition table
    db.execute(db.queries['create_bc_boundary'])

    # Prep boundary sources
    # First, combine ABMS boundary and marine ecosections
    db["bc_boundary_marine"].drop()
    db.execute("""CREATE TABLE a00_bc_boundary_marine AS
                  SELECT
                    'bc_boundary_marine' as designation,
                     ST_Union(geom) as geom FROM
                      (SELECT st_union(geom) as geom
                       FROM a00_bc_abms
                       UNION ALL
                       SELECT st_union(geom) as geom
                       FROM a00_marine_ecosections) as foo
                   GROUP BY designation""")

    for source in ["a00_bc_boundary_land", "a00_bc_boundary_marine"]:
        info('Prepping and inserting into bc_boundary: %s' % source)
        # subdivide before attempting to tile
        db["temp_"+source].drop()
        db.execute("""CREATE UNLOGGED TABLE temp_{t} AS
                      SELECT ST_Subdivide(geom) as geom
                      FROM {t}""".format(t=source))
        db["temp_"+source].create_index_geom()
        # tile
        db[source+"_tiled"].drop()
        lookup = {"src_table": "temp_"+source,
                  "out_table": source+"_tiled"}
        db.execute(db.build_query(db.queries["prep1_merge_tile_b"], lookup))
        db["temp_"+source].drop()

        # combine the boundary layers into new table bc_boundary
        sql = db.build_query(db.queries["populate_output"],
                             {"in_table": source+"_tiled",
                              "out_table": "bc_boundary"})
        tiles = get_tiles(db, source+"_tiled")
        func = partial(parallel_tiled, sql)
        pool = multiprocessing.Pool(processes=n_processes)
        pool.map(func, tiles)
        pool.close()
        pool.join()

    # rename the 'designation' column
    db.execute("""ALTER TABLE bc_boundary
                  RENAME COLUMN designation TO bc_boundary""")


def intersect(db, in_table, intersect_table, out_table, n_processes,
              tiles=None):
    """
    Intersect in_table with intersect_table, creating out_table
    Inputs may not have equivalently named columns
    """
    # examine the inputs to determine what columns should be in the output
    in_columns = [Column(c.name, c.type) for c in db[in_table].sqla_columns]
    intersect_columns = [Column(c.name, c.type)
                         for c in db[intersect_table].sqla_columns
                         if c.name not in ['geom', 'map_tile']]
    # make sure output column names are unique, removing geom and map_tile from
    # the list as they are hard coded into the query
    in_names = set([c.name for c in in_columns
                    if c.name != 'geom' and c.name != 'map_tile'])
    intersect_names = set([c.name for c in intersect_columns])

    # test for non-unique columns in input (other than map_tile and geom)
    non_unique_columns = in_names.intersection(intersect_names)
    if non_unique_columns:
        info('Column(s) found in both sources: %s' %
             ",".join(non_unique_columns))
        raise Exception("Input column names must be unique")
    # create output table
    db[out_table].drop()
    # add primary key
    pk = Column(out_table+"_id", Integer, primary_key=True)
    pgdb.Table(db, "public", out_table, [pk]+in_columns+intersect_columns)
    # populate the output table
    if 'map_tile' not in [c.name for c in db[intersect_table].sqla_columns]:
        query = "intersect_inputtiled"
        tile_table = "tiles"
        sql = db.build_query(db.queries[query],
                             {"in_table": in_table,
                              "in_columns": ", ".join(in_names),
                              "intersect_table": intersect_table,
                              "intersect_columns": ", ".join(intersect_names),
                              "out_table": out_table,
                              "tile_table": tile_table})
    else:
        query = "intersect_alltiled"
        tile_table = None
        sql = db.build_query(db.queries[query],
                             {"in_table": in_table,
                              "in_columns": ", ".join(in_names),
                              "intersect_table": intersect_table,
                              "intersect_columns": ", ".join(intersect_names),
                              "out_table": out_table})
    if not tiles:
        tiles = get_tiles(db, intersect_table, "tiles")
    func = partial(parallel_tiled, sql)
    pool = multiprocessing.Pool(processes=n_processes)

    # add a progress bar
    results_iter = pool.imap_unordered(func, tiles)
    with click.progressbar(results_iter, length=len(tiles)) as bar:
        for _ in bar:
            pass

    # pool.map(func, tiles)
    pool.close()
    pool.join()
    # delete any records with empty geometries in the out table
    db.execute("""DELETE FROM {t} WHERE ST_IsEmpty(geom) = True
               """.format(t=out_table))
    # add map_tile index to output
    db.execute("""CREATE INDEX {t}_tileix
                  ON {t} (map_tile text_pattern_ops)
               """.format(t=out_table))


def tidy_designations(db, sources, designation_key, out_table):
    """Add and populate 'category' column, tidy the national park designations
    """
    # add category (rollup) column by creating lookup table from source.csv
    lookup_data = [dict(alias=s[designation_key],
                        category=s["category"])
                   for s in sources if s["category"]]
    # create lookup table
    db["category_lookup"].drop()
    db.execute("""CREATE TABLE category_lookup
                  (id SERIAL PRIMARY KEY, alias TEXT, category TEXT)""")
    db["category_lookup"].insert(lookup_data)

    # add category column
    if "category" not in db[out_table].columns:
        db.execute("""ALTER TABLE {t}
                      ADD COLUMN category TEXT
                   """.format(t=out_table))

    # populate category column from lookup
    db.execute("""UPDATE {t} AS o
                  SET category = lut.category
                  FROM category_lookup AS lut
                  WHERE o.designation = lut.alias
               """.format(t=out_table))

    # Remove national park names from the national park tags
    sql = """UPDATE {t}
             SET designation = 'c01_park_national'
             WHERE designation LIKE 'c01_park_national%%'
          """.format(t=out_table)
    db.execute(sql)


def get_layer_name(file, layer_name):
    """
    Check number of layers and only use layer name from sources.csv
    if > 1 layer, else use first
    """
    layers = fiona.listlayers(file)
    # replace the . with _ in WHSE objects
    if re.match("^WHSE_", layer_name):
        layer_name = re.sub("\\.", "_", layer_name)

    if len(layers) > 1:
        if layer_name not in layers:
            # try looking if there is a layer called layername_polygon
            if layer_name + '_polygon' in layers:
                layer = layer_name + '_polygon'
            else:
                raise Exception("cannot find layer name")
        else:
            layer = layer_name
    else:
        layer = layers[0]
    return layer


# --------------
# CLI
# --------------
def validate_tablename(ctx, param, value):
    return value.lower()


@click.group()
def cli():
    pass


@cli.command()
def create_db():
    """Create a fresh database
    """
    pgdb.create_db(CONFIG["db_url"])
    db = pgdb.connect(CONFIG["db_url"])
    db.execute("CREATE EXTENSION postgis")
    db.execute("CREATE EXTENSION lostgis")


@cli.command()
@click.option('--source_csv', '-s', default=CONFIG["source_csv"],
              type=click.Path(exists=True), help=HELP['csv'])
@click.option('--email', help=HELP['email'])
@click.option('--dl_path', default=CONFIG["source_data"],
              type=click.Path(exists=True), help=HELP['dl_path'])
@click.option('--alias', '-a', help=HELP['alias'])
@click.option('--force_download', default=False, help='Force fresh download')
def load(source_csv, email, dl_path, alias, force_download):
    """Download data, load to postgres
    """
    db = pgdb.connect(CONFIG["db_url"])
    sources = read_csv(source_csv)

    # filter sources based on optional provided alias and ignoring excluded
    if alias:
        sources = [s for s in sources if s["alias"] == alias]
    sources = [s for s in sources if s["exclude"] != 'T']

    # process sources where automated downloads are avaiable
    load_commands = []
    for source in [s for s in sources if s["manual_download"] != 'T']:

        # handle BCGW downloads
        if urlparse(source["url"]).hostname == 'catalogue.data.gov.bc.ca':
            gdb = source["layer_in_file"].split(".")[1].strip() + ".gdb"
            file = os.path.join(dl_path, gdb)
            # download only if layer is not already there or if forced
            if not os.path.exists(file) and not force_download:
                download_bcgw(source["url"], dl_path, email=email, gdb=gdb)

        # handle all other downloads
        else:
            if os.path.exists(os.path.join(dl_path, source["alias"])):
                file = os.path.join(dl_path, source["alias"],
                                    source['file_in_url'])
            else:
                download_cache = os.path.join(tempfile.gettempdir(), "dl_cache")
                if not os.path.exists(download_cache):
                    os.makedirs(download_cache)
                fp = download_non_bcgw(source['url'], download_cache)
                # * here we assume that all non-bcgw downloads are zip files *
                file = extract(fp,
                               dl_path,
                               source['alias'],
                               source['file_in_url'])

        layer = get_layer_name(file, source["layer_in_file"])
        load_commands.append(db.ogr2pg(file, in_layer=layer,
            out_layer=source["input_table"], sql=source["query"], cmd_only=True))

    # process manually downloaded sources
    for source in [s for s in sources if s["manual_download"] == 'T']:
        file = os.path.join(dl_path, source["file_in_url"])
        if not os.path.exists(file):
            raise Exception(file + " does not exist, download it manually")
        layer = get_layer_name(file, source["layer_in_file"])
        load_commands.append(db.ogr2pg(file, in_layer=layer,
            out_layer=source["input_table"], sql=source["query"], cmd_only=True))

    # run all ogr commands in parallel
    info('loading data to postgres...')
    # https://stackoverflow.com/questions/14533458/python-threading-multiple-bash-subprocesses
    processes = [subprocess.Popen(cmd, shell=True) for cmd in load_commands]
    for p in processes: p.wait()

    # create tiles layer
    db.execute(db.queries["create_tiles"])


@cli.command()
@click.option('--source_csv', '-s', default=CONFIG["source_csv"],
              type=click.Path(exists=True), help=HELP['csv'])
@click.option('--out_table', '-ot', callback=validate_tablename,
              default=CONFIG["out_table"], help=HELP["out_table"])
@click.option('--resume', '-r',
              help='hierarchy number at which to resume processing')
@click.option('--force_preprocess', is_flag=True, default=False,
              help="Force re-preprocessing of input data")
@click.option('--n_processes', '-p', default=CONFIG["n_processes"],
              help="Number of parallel processing threads to utilize")
@click.option('--tiles', default=None,
              help="Comma separated list of tiles to process")
def process(source_csv, out_table, resume, force_preprocess, n_processes,
            tiles):
    """Create output designatedlands tables
    """
    out_table = out_table.lower()

    db = pgdb.connect(CONFIG["db_url"], schema="public")

    # run required preprocessing, tile, attempt to clean inputs
    preprocess(db, source_csv, force=force_preprocess)
    tile_sources(db, source_csv, force=force_preprocess)
    clean_and_agg_sources(db, source_csv, force=force_preprocess)

    # translate tile arg to 20k tiles (allows passing values like '093')
    if tiles:
        arg_tiles = []
        for tile_token in tiles.split(","):
            sql = """
                  SELECT DISTINCT map_tile
                  FROM tiles
                  WHERE map_tile LIKE %s"""
            tiles20 = [r[0] for r in db.query(sql, (tile_token+'%',)).fetchall()]
            arg_tiles = arg_tiles + tiles20
    else:
        arg_tiles = None

    # create target tables if not resuming from a bailed process
    if not resume:
        # create output tables
        db.execute(db.build_query(db.queries["create_outputs_prelim"],
                                  {"table": out_table}))
    # filter sources - use only non-exlcuded sources with hierarchy > 0
    sources = [s for s in read_csv(source_csv)
               if s['hierarchy'] != 0 and s["exclude"] != 'T']

    # To create output table with overlaps, simply combine all source data
    # (tiles argument does not apply, we could build a tile query string but
    # it seems unnecessary)
    for source in sources:
        info("Inserting %s into preliminary output overlap table" % source["tiled_table"])
        sql = db.build_query(db.queries["populate_output_overlaps"],
                             {"in_table": source["tiled_table"],
                              "out_table": out_table+"_overlaps"})
        db.execute(sql)

    # To create output table with no overlaps, more processing is required
    # In case of bailing during tests/development, `resume` option is available
    # to enable resumption of processing at specified hierarchy number
    if resume:
        p_sources = [s for s in sources if int(s["hierarchy"]) >= int(resume)]
    else:
        p_sources = sources

    # The tiles layer will fill in gaps between sources (so all BC is included
    # in output). To do this, first match schema of tiles to other sources
    db.execute("ALTER TABLE tiles ADD COLUMN IF NOT EXISTS id integer")
    db.execute("UPDATE tiles SET id = tile_id")
    db.execute("ALTER TABLE tiles ADD COLUMN IF NOT EXISTS designation text")
    # Next, add simple tiles layer definition to sources list
    p_sources.append({"cleaned_table": "tiles",
                      "category": None})
    # iterate through all sources
    for source in p_sources:
        sql = db.build_query(db.queries["populate_output"],
                             {"in_table": source["cleaned_table"],
                              "out_table": out_table+"_prelim"})
        # determine which specified tiles are present in source layer
        src_tiles = set(get_tiles(db, source["cleaned_table"],
                                  tile_table='a00_tiles_20k'))

        if arg_tiles:
            tiles = set(arg_tiles) & src_tiles
        else:
            tiles = src_tiles

        if tiles:
            info("Inserting %s into preliminary output table" % source["cleaned_table"])
            # for testing, run only one process and report on tile
            if n_processes == 1:
                for tile in tiles:
                    info(tile)
                    db.execute(sql, (tile + "%", ) * 2)
            else:
                func = partial(parallel_tiled, sql, n_subs=2)
                pool = multiprocessing.Pool(processes=n_processes)
                pool.map(func, tiles)
                pool.close()
                pool.join()

    # create marine-terrestrial layer
    if 'bc_boundary' not in db.tables:
        create_bc_boundary(db, n_processes)

    info('Cutting %s with marine-terrestrial definition' % out_table)
    intersect(db, out_table+"_prelim", "bc_boundary", out_table, n_processes,
              tiles)

    tidy_designations(db, sources, "cleaned_table", out_table)
    tidy_designations(db, sources, "cleaned_table", out_table+"_overlaps")


@cli.command()
@click.argument('in_file', type=click.Path(exists=True))
@click.option('--dl_table', '-dl', callback=validate_tablename,
              default=CONFIG["out_table"], help=HELP["out_table"])
@click.option('--in_layer', '-l', help="Input layer name")
@click.option('--dump_file', is_flag=True, default=False,
              help="Dump to file (as specified by out_file and out_format)")
@click.option('--out_file', '-o', default=CONFIG["out_file"],
              help=HELP["out_file"])
@click.option('--out_format', '-of', default=CONFIG["out_format"],
              help=HELP["out_format"])
@click.option('--new_layer_name', '-nln', help="Output layer name")
@click.option('--n_processes', '-p', default=CONFIG["n_processes"],
              help="Number of parallel processing threads to utilize")
def overlay(in_file, dl_table, in_layer, dump_file, out_file, out_format,
            aggregate_fields, new_layer_name, n_processes):
    """Intersect layer with designatedlands"""
    # load in_file to postgres
    db = pgdb.connect(CONFIG["db_url"], schema="public")
    if not in_layer:
        in_layer = fiona.listlayers(in_file)[0]
    if not new_layer_name:
        new_layer_name = in_layer[:63]  # Maximum table name length is 63

    out_layer = new_layer_name[:50] + "_overlay"

    db.ogr2pg(in_file, in_layer=in_layer, out_layer=new_layer_name)
    # pull distinct tiles iterable into a list
    tiles = [t for t in db["tiles"].distinct('map_tile')]
    # uncomment and adjust for debugging a specific tile
    # tiles = [t for t in tiles if t[:4] == '092K']
    info("Intersecting %s with %s" % (dl_table, new_layer_name))
    intersect(db, dl_table, new_layer_name, out_layer, n_processes, tiles)

    if dump_file:
        # dump result to file
        info("Dumping intersect to file %s " % out_file)
        dump(out_layer, out_file, out_format)


@cli.command()
@click.option('--dump_table', '-t', callback=validate_tablename,
              default=CONFIG["out_table"], help="Name of table to dump")
@click.option('--out_file', '-o', default=CONFIG["out_file"],
              help=HELP["out_file"])
@click.option('--out_format', '-of', default=CONFIG["out_format"],
              help=HELP["out_format"])
def dump(dump_table, out_file, out_format):
    """Dump output designatedlands table to file
    """
    db = pgdb.connect(CONFIG["db_url"], schema="public")
    info('Dumping %s to %s' % (dump_table, out_file))
    columns = [c for c in db[dump_table].columns if c != 'geom']
    ogr_sql = """SELECT {cols},
                  st_snaptogrid(geom, .001) as geom
                FROM {t}
                WHERE designation IS NOT NULL
             """.format(cols=",".join(columns),
                        t=dump_table)
    info(ogr_sql)
    db = pgdb.connect(CONFIG["db_url"])
    db.pg2ogr(ogr_sql, out_format, out_file, dump_table,
              geom_type="MULTIPOLYGON")


@cli.command()
@click.option('--dump_table', '-t', callback=validate_tablename,
              default=CONFIG["out_table"], help="Name of table to dump")
@click.option('--new_layer_name', '-nln', help="Output layer name")
@click.option('--out_file', '-o', default=CONFIG["out_file"],
              help=HELP["out_file"])
@click.option('--out_format', '-of', default=CONFIG["out_format"],
              help=HELP["out_format"])
def dump_aggregate(out_table, new_layer_name, out_file, out_format):
    """Unsupported
    """
    """test aggregation of designatedlands over tile boundaries

    Output data is aggregated across map tiles to remove gaps introduced in
    tiling of the sources. Aggregation is by distinct 'designation' in the
    output layer, and is run separately for each designation for speed.
    To dump data aggregated by 'category' or some other field, build and run
    your own ogr2ogr command based on below queries.

    This command is unsupported, aggregation does not quite remove gaps across all
    records and is very slow. Use mapshaper to aggregate outputs from the dump
    command (convert to shapefile first)

    eg:
    $ mapshaper designatedlands.shp \
        -clean snap-interval=0.01 \
        -dissolve designatio copy-fields=category \
        -explode \
        -o dl_clean.shp
    """
    db = pgdb.connect(CONFIG["db_url"], schema="public")
    info('Aggregating %s to %s' % (out_table, new_layer_name))
    # find all non-null designations
    designations = [d for d in db[out_table].distinct('designation') if d]
    db[new_layer_name].drop()
    sql = """CREATE TABLE {new_layer_name} AS
             SELECT designation, category, bc_boundary, geom
             FROM {out_table}
             LIMIT 0""".format(new_layer_name=new_layer_name,
                               out_table=out_table)
    db.execute(sql)
    # iterate through designations to speed up the aggregation
    for designation in designations:
        info('Adding %s to %s' % (designation, new_layer_name))
        # dump records entirely within a tile
        sql = """
        INSERT INTO {new_layer_name} (designation, category, bc_boundary, geom)
        SELECT
          dl.designation,
          dl.category,
          dl.bc_boundary,
          dl.geom
        FROM {t} dl
        INNER JOIN tiles ON dl.map_tile = tiles.map_tile
        WHERE dl.designation = %s
        AND ST_Coveredby(dl.geom, ST_Buffer(tiles.geom, -.01))
        """.format(t=out_table,
                   new_layer_name=new_layer_name)
        db.execute(sql, (designation,))
        # aggregate cross-tile records
        # Notes:
        # - expansion/contraction buffering of 3mm to remove gaps between tiles
        # - ST_Buffer of 0 on ST_Collect is much faster than ST_Union
        # - ST_Collect is a bit less robust, it requires ST_RemoveRepeatedPoints
        #   to complete successfully on sources that appear to come from rasters
        #   (mineral_reserve, ogma_legal)
        sql = """
        INSERT INTO {new_layer_name} (designation, category, bc_boundary, geom)
        SELECT
          designation,
          category,
          bc_boundary,
          (ST_Dump(ST_Buffer(geom, -.003))).geom as geom
        FROM (
          SELECT
            dl.designation,
            dl.category,
            dl.bc_boundary,
            ST_Buffer(
              ST_RemoveRepeatedPoints(
                ST_SnapToGrid(
                  ST_Collect(
                    ST_Buffer(dl.geom, .003)), .001), .01), 0) as geom
          FROM designatedlands dl
        INNER JOIN tiles ON dl.map_tile = tiles.map_tile
        WHERE dl.designation = %s
        AND NOT ST_Coveredby(dl.geom, ST_Buffer(tiles.geom, -.01))
        GROUP BY dl.designation, dl.category, dl.bc_boundary) as foo
        """.format(t=out_table,
                   new_layer_name=new_layer_name)
        db.execute(sql, (designation,))
    info('Dumping %s to file %s', (new_layer_name, out_file))
    db.pg2ogr("SELECT * from "+new_layer_name, out_format, out_file, new_layer_name)


@cli.command()
@click.option('--source_csv', '-s', default=CONFIG["source_csv"],
              type=click.Path(exists=True), help=HELP['csv'])
@click.option('--email', help=HELP['email'])
@click.option('--dl_path', default=CONFIG["source_data"],
              type=click.Path(exists=True), help=HELP['dl_path'])
@click.option('--out_table', '-ot', callback=validate_tablename,
              default=CONFIG["out_table"], help=HELP['out_table'])
@click.option('--out_file', '-o', default=CONFIG["out_file"], help=HELP['out_file'])
@click.option('--out_format', '-of', default=CONFIG["out_format"],
              help=HELP["out_format"])
def run_all(source_csv, email, dl_path, out_table, out_file, out_format):
    """ Run complete designated lands job
    """
    create_db()
    load(source_csv, email, dl_path)
    process(source_csv, out_table)
    dump(out_file, out_format)


if __name__ == '__main__':
    cli()

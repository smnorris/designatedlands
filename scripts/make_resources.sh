#!/bin/sh

# Converts both outputs (with and without overlaps) from gpkg into shapefiles, dumps
# them into a new directory, and zips them up. Also zips up the gpkg.
# End up with two zipfiles to be uploaded to the GH release

DATE=`date +%Y-%m-%d`
newdir=out_$DATE

mkdir $newdir

ogr2ogr $newdir/designatedlands.shp -sql \
  "SELECT designatedlands_id as dl_id, 
     category, 
     designation as desig, 
     bc_boundary as bc_bound, 
     map_tile, 
     geom 
   FROM designatedlands;" \
   designatedlands.gpkg \
   -lco ENCODING=UTF-8

ogrinfo $newdir/designatedlands.shp -sql "CREATE SPATIAL INDEX ON designatedlands" 

ogr2ogr $newdir/designatedlands_overlaps.shp -sql \
  "SELECT designatedlands_overlaps_id as dl_ol_id,
    category,
    designation as desig,
    designation_id as desig_id,
    designation_name as desig_name,
    map_tile,
    bc_boundary as bc_bound,
    geom
  FROM designatedlands_overlaps;" \
  designatedlands.gpkg \
  -lco ENCODING=UTF-8

ogrinfo $newdir/designatedlands_overlaps.shp -sql "CREATE SPATIAL INDEX ON designatedlands_overlaps" 

zip -j $newdir/designatedlands.shp.zip \
  $newdir/designatedlands.shp \
  $newdir/designatedlands.dbf \
  $newdir/designatedlands.shx \
  $newdir/designatedlands.prj \
  $newdir/designatedlands.qix \
  $newdir/designatedlands_overlaps.shp \
  $newdir/designatedlands_overlaps.dbf \
  $newdir/designatedlands_overlaps.shx \
  $newdir/designatedlands_overlaps.prj \
  $newdir/designatedlands_overlaps.qix

zip -j $newdir/designatedlands.gpkg.zip designatedlands.gpkg

find $newdir ! -name '*.zip' -delete

echo "Before uploading to release, open the shapefiles in ArcCatalog and add an Arc spatial index, 
since ArcGIS doesn't read the ogr spatial index (.qix). Right-click on the shapefile in ArCatalog, 
choose 'Indexes' tab, and click 'Add'. Then re-zip."

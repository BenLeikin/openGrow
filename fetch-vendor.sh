#!/usr/bin/env bash
# Self-host openGardener's frontend libraries and fonts, removing the CDN
# dependency so the dashboard works without internet on the viewing device
# and nothing is loaded cross-origin on a public-facing site.
#
# Run on the Pi:  bash fetch_vendor.sh
# Then swap the CDN <script>/<link> tags for the /static/vendor/ ones
# (the accompanying index.html already points at these paths).

set -e
VENDOR=~/static/vendor
FONTS=$VENDOR/fonts
mkdir -p "$VENDOR" "$FONTS"

echo "fetching JS libraries..."
dl() { echo "  $2"; curl -fsSL "$1" -o "$VENDOR/$2"; }

dl "https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" chart.umd.min.js
dl "https://cdn.jsdelivr.net/npm/luxon@3.4.4/build/global/luxon.min.js" luxon.min.js
dl "https://cdn.jsdelivr.net/npm/chartjs-adapter-luxon@1.3.1/dist/chartjs-adapter-luxon.umd.min.js" chartjs-adapter-luxon.umd.min.js
dl "https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js" hammer.min.js
dl "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js" chartjs-plugin-zoom.min.js
dl "https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1/dist/chartjs-plugin-annotation.min.js" chartjs-plugin-annotation.min.js

echo "fetching fonts..."
# Pull the Google Fonts CSS with a browser UA so it returns woff2, rewrite the
# font URLs to local, and download each font file.
UA="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
CSS_URL="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,400;9..144,600;9..144,900&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap"

curl -fsSL -A "$UA" "$CSS_URL" -o "$FONTS/fonts.css"

# extract font file URLs, download them, and rewrite the css to point local
grep -oE "https://[^)]+\.woff2" "$FONTS/fonts.css" | sort -u | while read -r url; do
  fname=$(basename "$url")
  echo "  $fname"
  curl -fsSL "$url" -o "$FONTS/$fname"
  # rewrite this URL in the css to a local path
  sed -i "s#$url#/static/vendor/fonts/$fname#g" "$FONTS/fonts.css"
done

echo "done. Vendored into $VENDOR"
echo "Fonts CSS: /static/vendor/fonts/fonts.css"

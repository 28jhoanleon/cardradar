#!/data/data/com.termux/files/usr/bin/bash
# actualizar.sh - agarra los archivos mas nuevos que bajaste del chat de
# Claude (aunque Android les haya puesto -1, -2, etc.), los copia al
# proyecto, y borra las copias de Descargas para que la proxima vez no
# se acumulen numeritos.
#
# USO:
#   ./actualizar.sh                    solo actualiza los archivos
#   ./actualizar.sh "mensaje commit"   ademas commitea y pushea

set -e
cd ~/cardradar
DL=~/storage/downloads

echo "Buscando archivos nuevos en Descargas..."
CAMBIO=0

for base in cards_data cards_app; do
  ultimo=$(ls -t "$DL"/${base}*.py 2>/dev/null | head -1)
  if [ -n "$ultimo" ]; then
    cp "$ultimo" ~/cardradar/${base}.py
    echo "  OK: ${base}.py <- $(basename "$ultimo")"
    CAMBIO=1
  else
    echo "  (no encontre ${base}*.py en Descargas, no lo toco)"
  fi
done

ultimo_req=$(ls -t "$DL"/requirements*.txt 2>/dev/null | head -1)
if [ -n "$ultimo_req" ]; then
  cp "$ultimo_req" ~/cardradar/requirements.txt
  echo "  OK: requirements.txt <- $(basename "$ultimo_req")"
  CAMBIO=1
fi

if [ "$CAMBIO" = "0" ]; then
  echo "No habia nada nuevo para copiar."
  exit 0
fi

echo ""
echo "Limpiando Descargas (asi la proxima vez no hay numeritos)..."
rm -f "$DL"/cards_data*.py "$DL"/cards_app*.py "$DL"/requirements*.txt

echo ""
echo "Verificando sintaxis..."
python3 -m py_compile cards_data.py cards_app.py
echo "sintaxis OK"

if [ -n "$1" ]; then
  echo ""
  echo "Commiteando y pusheando..."
  git add cards_data.py cards_app.py requirements.txt
  git commit -m "$1"
  git push
  echo "Listo, deploy disparado en Railway."
else
  echo ""
  echo "Archivos actualizados localmente. Si esta todo bien, corre:"
  echo "  git add cards_data.py cards_app.py requirements.txt"
  echo "  git commit -m \"tu mensaje\""
  echo "  git push"
fi

#!/usr/bin/env bash
# Test de fumée d'une image : elle démarre durcie (lecture seule, aucun privilège), survit à des
# outils injoignables, écrit sa base et sa sauvegarde en privé, puis s'arrête proprement.
#
#   scripts/test-fumee.sh <image>        par exemple : scripts/test-fumee.sh dolop:local
set -euo pipefail

image="${1:?usage : scripts/test-fumee.sh <image>}"
nom="dolop-fumee-$$"
volume="dolop-fumee-$$"

nettoyer() {
  docker rm -f "$nom" >/dev/null 2>&1 || true
  docker volume rm "$volume" >/dev/null 2>&1 || true
}
trap nettoyer EXIT

# Journal lu en entier AVANT d'y chercher : « docker logs | grep -q » échoue au hasard avec
# pipefail (grep -q s'arrête à la première ligne trouvée, docker logs reçoit SIGPIPE).
journal() {
  docker logs "$nom" 2>&1
}

echec() {
  echo "ÉCHEC : $*" >&2
  journal | tail -20 >&2 || true
  exit 1
}

docker run --rm "$image" dolop --version

docker volume create "$volume" >/dev/null
docker run --rm --user 0 --entrypoint chown -v "$volume:/data" "$image" 1000:1000 /data

docker run -d --name "$nom" \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true \
  --pids-limit 64 --memory 256m \
  -v "$volume:/data" \
  -e DOLIBARR_URL=http://127.0.0.1:9 -e DOLIBARR_API_KEY=fumee \
  -e OPENPROJECT_URL=http://127.0.0.1:9 -e OPENPROJECT_API_KEY=fumee \
  -e INTERVALLE_SECONDES=10 \
  "$image" >/dev/null

for _ in $(seq 1 30); do
  # La sauvegarde quotidienne suit le premier cycle : l'attendre évite de la chercher trop tôt.
  grep -q "base d'état sauvegardée" <<<"$(journal)" && break
  sleep 1
done
grep -q "service .* démarré" <<<"$(journal)" || echec "le service n'a pas démarré"
grep -q "cycle 1 : échec" <<<"$(journal)" || echec "aucun cycle en 30 s"
grep -q "base d'état sauvegardée" <<<"$(journal)" || echec "aucune sauvegarde après le premier cycle"
[ "$(docker inspect -f '{{.State.Running}}' "$nom")" = true ] || echec "le service s'est arrêté après une panne"

if docker exec "$nom" dolop sante; then echec "sonde de santé verte sans aucun cycle réussi"; fi

droits=$(docker exec "$nom" stat -c '%a' /data/etat.sqlite)
[ "$droits" = 600 ] || echec "base d'état lisible par d'autres (droits $droits)"
docker exec "$nom" sh -c 'ls /data/sauvegardes/etat-*.sqlite' >/dev/null || echec "aucune sauvegarde"
docker exec "$nom" dolop rapport >/dev/null || echec "« dolop rapport » en échec"

docker stop -t 60 "$nom" >/dev/null
code=$(docker inspect -f '{{.State.ExitCode}}' "$nom")
[ "$code" = 0 ] || echec "arrêt avec le code $code"
grep -q "arrêté proprement" <<<"$(journal)" || echec "arrêt non propre"

echo "Test de fumée réussi : $image"

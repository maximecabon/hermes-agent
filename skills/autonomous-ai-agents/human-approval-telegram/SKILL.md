---
name: human-approval-telegram
description: "Use when a workflow explicitly requires a human validation, approval, refusal, Go/No-Go, sign-off, or authorization before continuing. Mirror the pending gate to Telegram with exactly Approuver and Refuser, correlate one authorized decision to the requesting flow, and fail closed on timeout or delivery failure. Do not use for ordinary technical verification, tests, lint, reviewer checks, factual questions, or low-stakes clarification."
version: 1.0.0
author: Maxime / Hermes Looper
license: MIT
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [human-in-the-loop, approval, telegram, correlation, fail-closed]
    related_skills: [hermes-agent]
---

# Validation humaine avec miroir Telegram

## But

Cette skill normalise les gates humaines explicites. Une demande d’approbation reste visible dans le canal d’origine et est aussi envoyée au canal Telegram d’approbation avec exactement deux boutons : `Approuver` et `Refuser`. La décision autorisée est corrélée à la requête, renvoyée au flux demandeur, puis consommée une seule fois.

Cette skill ne constitue pas, à elle seule, le transport ni le mécanisme de reprise. Elle exige une intégration Hermes dédiée telle que définie dans « Prérequis techniques ». Si cette intégration est absente, ne pas simuler les boutons et ne pas considérer une réponse comme une approbation.

## Quand l’utiliser

Déclencher la gate quand le workflow, l’utilisateur, un contrat opératoire, une carte ou une politique exige explicitement une décision humaine avant de continuer, par exemple :

- « fais-moi valider avant d’exécuter » ;
- approbation, autorisation, sign-off, Go/No-Go, acceptation ou refus par Maxime ;
- publication, envoi, déploiement, engagement, changement de phase ou action irréversible conditionné à un accord humain ;
- résultat automatisé qui doit être accepté par une personne avant de devenir décisionnel ;
- approbation manuelle native d’un outil si l’intégration technique la classe dans ce protocole commun.

Ne pas déclencher pour :

- exécuter tests, lint, build, calculs, contrôles de cohérence ou vérifications techniques ;
- demander à `reviewer` de vérifier un livrable contre des critères ;
- constater qu’une preuve est présente ou absente ;
- poser une question factuelle, demander une préférence ou clarifier une ambiguïté sans gate d’autorisation ;
- annoncer qu’un travail est terminé sans action ultérieure conditionnée ;
- auto-évaluer sa propre réponse.

Test de qualification : si les deux réponses possibles sont réellement « l’humain autorise la suite » ou « l’humain interdit/arrête la suite », utiliser cette skill. Sinon utiliser la vérification technique, la revue ou `clarify` ordinaire.

## Prérequis techniques obligatoires

Avant la première gate, vérifier qu’Hermes expose une capacité dédiée de demande d’approbation humaine qui satisfait tout le contrat suivant :

1. envoi dans le canal d’origine sans le remplacer ;
2. envoi au canal Telegram d’approbation configuré côté opérateur ;
3. boutons Telegram libellés exactement `Approuver` et `Refuser`, sans troisième choix, sans emoji ajouté au libellé ;
4. identifiant de requête imprévisible et corrélation côté serveur avec le profil, la session et le tour demandeurs ;
5. contrôle de l’utilisateur Telegram autorisé et du chat cible ;
6. transition atomique d’un état `pending` vers un seul état terminal ;
7. retour de la décision au tool call ou au flux demandeur ;
8. timeout, annulation, redémarrage et indisponibilité traités sans approbation implicite ;
9. contenu redacted avant transport et journalisation ;
10. preuve structurée retournée au demandeur.

Le nom exact de l’outil doit être celui réellement installé et inspecté. Le contrat recommandé pour l’intégration est un outil dédié `request_human_approval`; ce nom n’est pas une capacité Hermes présumée. Tant que l’outil n’existe pas dans la liste réelle des outils, l’état est `blocked_missing_integration`.

Ne pas remplacer cette intégration par :

- `clarify` seul : son rendu Telegram ajoute actuellement un choix « Other » et cible la session gateway d’origine ;
- un simple message Telegram : il n’offre ni boutons corrélés ni retour fiable au flux ;
- `send_message` invoqué par le modèle : dans Hermes v0.18.2 inspecté, ce transport n’est volontairement pas enregistré comme outil agent-callable ;
- une réponse LLM déduite, un silence, un timeout, un test réussi ou l’avis d’un autre agent.

## Entrées minimales de la gate

Fournir à l’intégration dédiée :

- `summary` : décision demandée, concise et sans secret ;
- `requested_action` : action exacte qui restera bloquée ;
- `reason` : pourquoi une approbation humaine est requise ;
- `evidence` : chemins, identifiants ou synthèse redacted nécessaires pour décider ;
- `expires_in` : durée positive bornée par la configuration opérateur ;
- contexte de corrélation fourni par Hermes, jamais inventé par le modèle : profil, session/session_key, turn_id, tool_call_id, canal/chat/thread d’origine.

Ne jamais placer dans le message : token, clé API, mot de passe, cookie, chaîne de connexion, contenu privé brut inutile ou commande contenant des identifiants non redacted. Référencer un chemin ou un artefact sûr plutôt que copier un corpus sensible.

## Loop obligatoire

1. **Qualifier.** Distinguer la gate humaine d’une simple vérification technique avec le test de qualification ci-dessus. Terminé quand le type est `human_gate` ou `technical_verification`.
2. **Préparer.** Construire une demande concise indiquant action, raison, conséquences d’Approuver, conséquences de Refuser, expiration et preuves sûres. Terminé quand aucune donnée secrète ou inutile n’est présente.
3. **Créer et livrer.** Appeler l’intégration dédiée une seule fois. Le canal d’origine reçoit l’état d’attente ; Telegram reçoit la même gate avec exactement les deux boutons. Terminé seulement si l’intégration retourne `pending` et des preuves de livraison d’origine et Telegram.
4. **Bloquer.** Ne pas exécuter l’action conditionnée pendant `pending`. Ne pas lancer une autre copie de la même gate après une erreur transitoire sans réutiliser la même clé d’idempotence.
5. **Résoudre.** Accepter uniquement une décision authentifiée et corrélée retournée par l’intégration :
   - `approved` : poursuivre uniquement l’action et le périmètre décrits dans la gate ;
   - `refused` : ne pas exécuter l’action, marquer le flux refusé ou demander un recadrage non décisionnel ;
   - `expired`, `cancelled`, `delivery_failed`, `telegram_unavailable`, `blocked_missing_integration` : ne pas exécuter l’action ; retourner un blocage explicite.
6. **Prouver.** Conserver dans le retour du flux les identifiants non secrets, l’état terminal, l’horodatage, le canal de décision et la preuve de corrélation. Terminé quand l’état du flux demandeur correspond à l’état terminal de la gate.

## Protocole de corrélation

L’intégration technique doit créer un `request_id` aléatoire d’au moins 128 bits et stocker côté serveur une entrée durable :

```text
request_id
idempotency_key
requester_profile
requester_session_id ou session_key
requester_turn_id
tool_call_id
origin_platform / origin_chat_id / origin_thread_id
telegram_chat_id / telegram_thread_id
state = pending|approved|refused|expired|cancelled|delivery_failed
created_at / expires_at / decided_at
decided_by_authorized_user_id
origin_message_id / telegram_message_id
sanitized_summary_hash
```

Le `callback_data` Telegram ne doit contenir ni secret ni texte métier. Format court recommandé : `ha:<request_id>:a` et `ha:<request_id>:r`, sous la limite Telegram de 64 octets. La table côté serveur fait foi.

La résolution applique un compare-and-set atomique `pending -> approved|refused`. Le premier clic autorisé gagne. Un clic répété, tardif, non autorisé, provenant d’un autre chat ou visant une requête expirée ne change pas l’état et reçoit un accusé « déjà résolu », « expiré » ou « non autorisé ». Après décision, les boutons sont retirés ou le message est édité pour afficher l’état terminal.

Si canal d’origine et Telegram désignent exactement le même chat/thread, une seule présentation interactive peut satisfaire les deux rôles pour éviter un doublon ; la corrélation reste unique. Sinon, les deux livraisons sont obligatoires.

## Timeout, panne et reprise

- Timeout : transition `pending -> expired`, jamais `approved`.
- Telegram non configuré, bot arrêté, cible absente ou envoi échoué : `telegram_unavailable` ou `delivery_failed`; le flux reste bloqué.
- Redémarrage : l’état durable est relu ; une gate encore valide peut reprendre son attente, une gate expirée est fermée.
- Processus demandeur interrompu : conserver la décision dans le store durable et la réinjecter seulement dans la session/queue corrélée ; ne jamais l’appliquer à une nouvelle session par proximité sémantique.
- Livraison partielle : ne pas poursuivre. Signaler quel canal a échoué sans divulguer ses secrets de configuration.
- Retry : utiliser l’`idempotency_key` originale ; ne pas créer plusieurs gates actives pour la même action.

## Sortie attendue du flux demandeur

```yaml
human_approval:
  status: approved|refused|expired|cancelled|delivery_failed|telegram_unavailable|blocked_missing_integration
  request_id: "<identifiant non secret>"
  requester_profile: "<profil>"
  requester_session: "<session corrélée>"
  origin_delivery: delivered|failed|same_as_telegram
  telegram_delivery: delivered|failed
  decision_channel: telegram|origin|null
  decided_at: "<horodatage ou null>"
  next_action: "continue_scoped_action|stop|retry_same_request|escalate_operator"
```

Ne jamais produire `approved` sans preuve d’une transition authentifiée et corrélée. L’absence de réponse n’est pas une approbation.

## Pièges

1. Confondre « vérifie que les tests passent » avec « attends mon approbation ».
2. Utiliser `clarify` et accepter le choix libre « Other » comme décision.
3. Envoyer Telegram après avoir déjà exécuté l’action conditionnée.
4. Exposer la commande brute ou un document sensible dans le message.
5. Corréler seulement par texte, timestamp ou nom de profil.
6. Autoriser n’importe quel utilisateur Telegram parce que le chat est connu.
7. Perdre la décision lors d’un redémarrage ou l’appliquer à la session suivante.
8. Considérer un échec de livraison, un timeout ou un silence comme consentement.

## Vérification

Avant de déclarer le dispositif prêt, produire des preuves réelles pour les cas suivants :

- gate nominale depuis un canal non Telegram : message d’origine + message Telegram + deux boutons exacts ;
- clic `Approuver` : un seul flux corrélé reprend et exécute uniquement l’action approuvée ;
- clic `Refuser` : l’action n’est pas exécutée ;
- double clic et clic tardif : aucune seconde transition ;
- utilisateur Telegram non autorisé : aucune résolution ;
- deux demandes concurrentes de deux profils : aucune inversion de décision ;
- timeout court : état expiré, aucune auto-approbation ;
- Telegram indisponible : flux bloqué, erreur redacted ;
- redémarrage entre envoi et clic : décision récupérée ou gate expirée proprement ;
- vérification technique ordinaire : aucune gate Telegram créée ;
- création d’un nouveau profil : la politique et la capacité dédiée sont présentes sans copie manuelle de SOUL.

La preuve minimale comprend : sortie de tests automatisés, identifiants de messages de test, états du store avant/après, et smoke end-to-end avec deux profils distincts. Ne jamais utiliser une action externe réellement dangereuse pour le smoke ; utiliser un marqueur temporaire inoffensif.

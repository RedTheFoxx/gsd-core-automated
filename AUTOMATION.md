# GSD automatisé depuis Python

Ce wrapper est un **hôte GSD autonome**, indépendant de Kilo, Cline et Claude Code.
Il lit les commandes dans `commands/gsd`, charge leurs `execution_context`, suit
les workflows de `gsd-core/workflows`, charge les agents dans `agents`, et exécute
les utilitaires réels via `node gsd-core/bin/gsd-tools.cjs`. Il ne réécrit pas les
workflows en Python et ne dépend pas de l'ancien export SDK.

## Démarrage

Prérequis : Python 3.11+, Node 24+, Git, Bash (Git Bash sous Windows), et un endpoint
Chat Completions avec **appels d'outils**. Une compatibilité avec la génération de
texte seule ne suffit pas. Le transport utilise le protocole
[Chat Completions / function calling](https://developers.openai.com/api/docs/guides/function-calling).
Il n'impose ni SDK OpenAI, ni modèle OpenAI, ni Responses API.

Depuis ce dépôt :

```sh
npm ci
npm run build
python -m pip install -e .
mkdir -p tmp/gsd-demo
```

Copier et adapter `examples/automation/config.toml` et `rules.toml` : endpoint,
nom de modèle, dossier du projet et commandes d'acceptation. Les chemins de la
configuration, y compris les options de chemin de la CLI lorsqu'une configuration
est fournie, sont résolus relativement au fichier de configuration.

```sh
export OPENAI_API_KEY="..."  # Facultatif pour un serveur local sans authentification
gsd-auto --config examples/automation/config.toml --check
gsd-auto --config examples/automation/config.toml
gsd-auto --config examples/automation/config.toml --prompt "Mon besoin initial"
```

Sans installation Python, remplacer `gsd-auto` par `python -m gsd_automated`.
Sous PowerShell, créer le répertoire avec `New-Item -ItemType Directory -Force
tmp/gsd-demo`, définir la clé avec `$env:OPENAI_API_KEY = "..."`, et configurer
`shell = "C:/Program Files/Git/bin/bash.exe"`. Python, Node et Git doivent être
accessibles dans le PATH du processus. Un chemin absolu est accepté pour `node`.

On peut remplacer `prompt` par `prompt_file = "besoin.md"` (UTF-8). Sans besoin
dans la configuration ni `--prompt`, une unique saisie initiale est demandée si
stdin est un terminal. Aucun `input()` n'est utilisé ensuite. En CI, un besoin
absent produit une erreur immédiate. JSON est également accepté pour la config
et les règles. Sans config : fournir `--rules`, `--workspace`, `--gsd-root`, puis
`OPENAI_BASE_URL`, `OPENAI_MODEL` et `--prompt`.

## Exécution et décisions

1. Le contrôle local vérifie le corpus, Git, Node, Bash et l'identité du runtime GSD.
2. Le LLM hôte reçoit `new-project`, ou `progress` si une roadmap existe déjà.
3. Il exécute les instructions et poursuit les phases jusqu'à la livraison.
4. `AskUserQuestion` est intercepté. Les règles `answers` sont essayées dans
   l'ordre, par sous-chaîne insensible à la casse dans la question sérialisée.
   Sinon, un LLM représentant l'utilisateur reçoit besoin, règles, décisions
   antérieures et contexte récent. Il répond à la place de l'humain.
5. Une réponse libre du LLM hôte est aussi transmise au représentant : elle ne
   termine jamais implicitement la tâche. Les sous-agents doivent utiliser
   `AskUserQuestion` pour leurs questions et retourner leur résultat en texte.
6. `Finish` déclenche les commandes d'acceptation configurées et une revue LLM
   distincte du besoin et des preuves lues sur disque. Les échecs retournent au
   LLM hôte pour correction. La revue est une appréciation LLM, pas une preuve
   formelle : fournir des commandes d'acceptation pour les critères mesurables.

Par défaut, hôte, représentant et sous-agents utilisent le même modèle et endpoint.
`llm.decision_model` et `llm.agent_models` permettent de différencier les modèles
sur cet endpoint. `llm.request_options` passe des options fournisseur additionnelles
sans pouvoir remplacer les messages ou les outils. Les requêtes 429/5xx réessayées
comptent dans le budget global, partagé entre tous les rôles.

## Outils et portée de cette version

`Read` (pagination en caractères), `Write`, `Edit` (occurrence unique), `Glob`,
`Grep` (texte littéral), `Bash`, `GSD` (argv sans shell), `SlashCommand`, `Agent`,
`AskUserQuestion`, `Finish`. Les sous-agents ont leur propre conversation et les
mêmes outils, sauf `Finish`. Leur exécution est **synchrone et séquentielle**,
y compris les vagues ; pas de parallélisme ni de worktrees automatiques.
Les workflows restent interprétés par le LLM, comme dans les hôtes habituels.
Les hooks Claude/Kilo/Cline, MCP, navigateur interactif et fonctionnalités
spécifiques à ces hôtes ne sont pas réimplémentés. Les fallbacks documentés GSD
restent disponibles via scripts ; une capacité indispensable indisponible doit
produire un blocage. La recherche `GSD websearch` nécessite sa propre configuration.

Les scripts reçoivent stdin fermé, un délai maximal et un arrêt de l'arbre des
processus en cas de dépassement. Sous Windows, si le système refuse `taskkill`,
le processus direct est tué ; l'arrêt des descendants ne peut alors être garanti.
Le shell est neuf à chaque appel ; `gsd_run`
est fourni automatiquement dans Bash. PowerShell peut être configuré, mais le LLM
devra traduire les extraits Bash ; Git Bash est préférable pour ce corpus.

Les outils de fichiers limitent leurs chemins au projet et au corpus. **Le shell
est une exécution de code locale avec les droits du processus, pas un bac à sable.**
Les règles guident les décisions LLM, elles ne constituent pas une politique OS.
Utiliser un conteneur ou une VM pour une isolation forte. La variable contenant
la clé LLM est retirée de l'environnement des scripts, mais ce n'est pas une
isolation contre un code qui lirait d'autres fichiers ou secrets de la machine.

## Suivi, reprise et statuts

Les événements et décisions sont dans `.gsd-auto/<run-id>/events.jsonl`, le résultat
final dans `result.json`, et l'état GSD reste dans `.planning/`. La trace contient
des contenus de projet : la conserver localement. La clé LLM configurée est
masquée dans les événements. Il n'y a pas d'exécution simultanée autorisée sur le
même projet : un verrou exclusif l'empêche. Après un arrêt brutal, supprimer
`.gsd-auto/run.lock` seulement après avoir vérifié que le PID enregistré est arrêté.

```sh
gsd-auto --config examples/automation/config.toml --resume
python -m unittest discover -s tests_python -v
```

La reprise crée une nouvelle conversation à partir des fichiers GSD et des
décisions enregistrées. Elle n'est pas une reprise exacte de pile ni une garantie
« exactement une fois » pour les commandes interrompues. Le LLM doit examiner
les modifications partielles avant de réessayer. Conserver le même besoin et les
mêmes règles pour une reprise cohérente.

Codes de sortie : `0` livraison acceptée (ou contrôle local réussi), `1` erreur
technique/budget, `2` blocage déclaré, `130` interruption clavier. `--check` ne
contacte pas le LLM et ne valide donc ni la clé ni la capacité d'appels d'outils.
Les limites arrêtent explicitement la tâche ; elles ne transforment pas une
exécution incomplète en succès.

## Validation de cette version

18 tests couvrent notamment le protocole HTTP Chat Completions, les questions
structurées et libres, les règles, les contextes de sous-agents, les erreurs
d'outils, les délais de processus, les budgets, le verrou, la reprise et la
validation finale. Un test lance la vraie CLI Python avec un endpoint simulé,
le corpus GSD du dépôt, `query init.new-project`, Git Bash, Node, un sous-agent
qui écrit un fichier et une commande d'acceptation réelle. Ce test nécessite
la compilation préalable de GSD ; sinon il est explicitement ignoré.

La compilation complète de GSD et ces 18 tests ont réussi sous Windows.
Aucun parcours avec un vrai LLM n'a été effectué : la qualité du suivi autonome
de toutes les phases et la compatibilité d'un fournisseur particulier restent
à valider sur le modèle choisi.

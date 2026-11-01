# git_cgallagher.inventory

Inventory plugin (and, later, modules) for the Ansible Inventory System.

```yaml
# inventory.yml
plugin: git_cgallagher.inventory.db
url: http://127.0.0.1:8000
# token: via AIS_TOKEN (phase 1)
# subtree: all.commercial.prod
# revision: 42
cache: true
cache_plugin: jsonfile
cache_connection: ~/.cache/ais
```

```bash
ansible-inventory -i inventory.yml --graph
ansible-inventory -i inventory.yml --host web01
```

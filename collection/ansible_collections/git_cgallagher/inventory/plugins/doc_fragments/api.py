# -*- coding: utf-8 -*-
# Copyright (c) 2026 Chris Gallagher
# MIT License (see LICENSE)
from __future__ import annotations


class ModuleDocFragment(object):
    # Connection options shared by every module.
    DOCUMENTATION = r"""
options:
  url:
    description:
      - Base URL of the Ansible Inventory System server, for example C(http://127.0.0.1:8000).
      - Defaults to the E(AIS_URL) environment variable.
    type: str
  token:
    description:
      - Bearer token. Defaults to the E(AIS_TOKEN) environment variable.
      - Mint one with C(ais token create) or C(POST /api/v1/tokens); writes need the C(editor) role.
    type: str
  validate_certs:
    description: Validate the server's TLS certificate.
    type: bool
    default: true
  timeout:
    description: HTTP timeout in seconds.
    type: int
    default: 30
  change:
    description:
      - Change id sent as the C(X-AIS-Change) header and recorded on every revision this task creates (modules that only
        read create none), for example a ticket number such as C(SRE-1234).
      - Give every task of one change the same id (C(module_defaults) for the C(git_cgallagher.inventory.inventory)
        group does that for a whole play); C(GET /api/v1/revisions?change=SRE-1234) lists them and
        M(git_cgallagher.inventory.revert) with O(git_cgallagher.inventory.revert#module:revert_change) undoes them
        together.
      - 1 to 64 characters from letters, digits and C(._:+@-), starting with a letter or digit. Defaults to the
        E(AIS_CHANGE) environment variable.
    type: str
"""

    # Options shared by the modules that write.
    WRITE = r"""
options:
  expect_revision:
    description:
      - Inventory revision the change is based on, sent as C(If-Match).
      - By default the module reads the server's current revision right before its first write and follows the
        revision each write returns. Set this to refuse the change if anyone wrote since you looked (409).
    type: int
  revision_message:
    description: Free-text message recorded on the revision(s) this task creates.
    type: str
notes:
  - Every write is optimistically locked. If the inventory moves between the module's read and its write
    (another editor, a UI edit), the server answers 409 and the module fails with both revisions; re-run the task.
  - Writes are audited server-side with C(source=module) and a per-task request id.
"""

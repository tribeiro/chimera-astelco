chimera_astelco plugin
=======================

ASTELCO telescopes drivers for the chimera observatory control system
https://github.com/astroufsc/chimera.

Usage
-----

The ASTELCO drivers come with a new type of instrument, TPL, which is responsible for the communication with the
TSI system. In order for the drivers to work at least TPL instrument must be defined. The user may either use one
TPL for all instruments or a TPL for each instrument (see furthermore how to configure them). The later may be usefull
to avoid the single point of failure, though experience shows that when one fails, all fails.

Installation
------------

Installation instructions. Dependencies, etc...

::

   pip install -U chimera_astelco

or

::

    pip install -U git+https://github.com/astroufsc/chimera_astelco.git


Configuration Example
---------------------

Here goes an example of the configuration to be added on ``chimera.config`` file.

::

    instrument:
        name: TPLConn01
        type: TPL
        user: admin
        password: admin
        tpl_host: 127.0.0.1 # Host IP
        tpl_port: 65432 # Host port

    instrument:
        name: TPLConn02
        type: TPL
        user: admin
        password: admin
        tpl_host: 127.0.0.1 # Host IP
        tpl_port: 65432 # Host port

    instrument:
        name: MyTelescope
        type: Astelco
        tpl: /TPL/TPLConn01

    instrument:
        name: MyFocuser
        type: AstelcoFocuser
        tpl: /TPL/TPLConn01 # uses same TPL as the telescope

    instrument:
        name: MyDome
        type: AstelcoDome
        tpl: /TPL/TPLConn02 # uses a different TPL




Contact
-------

For more information, contact us on chimera's discussion list:
https://groups.google.com/forum/#!forum/chimera-discuss

Bug reports and patches are welcome and can be sent over our GitHub page:
https://github.com/astroufsc/chimera_template/

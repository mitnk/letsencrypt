Let's Encrypt Client for Nginx
==============================

Disclaimer
----------

This is a **Hack Version** based on the Let's Encrypt offical Client.
It's targeted on Nginx. Only support Python 2.7


Preparation
-----------

The **Preparation** only need to do once. Jump to **Usage** section
if you've already done it.

1) **Checkout the code & build the virtualenv**

::

    $ git clone https://github.com/mitnk/letsencrypt
    $ cd letsencrypt
    $ ./install.sh

This will create an virtualenv at ``~/.local/share/letsencrypt``.

2) **Setup Nginx Configs**

Put the following code into every domain server config
in you Nginx.

*Note: Please change `mitnk` to your username*

::

    location /.well-known/acme-challenge/ {
        default_type text/plain;
        alias /home/mitnk/.well-known/acme-challenge/;
    }

Create the directories:

::

    $ mkdir -p /home/mitnk/.well-known/acme-challenge/

Then reload Nginx (e.g. ``sudo nginx -s reload``).


Usage
-----


Enter virtualenv:

::

    $ sudo /home/mitnk/.local/share/letsencrypt/bin/letsencrypt --manual-public-ip-logging-ok --renew-by-default -d hugo.wang -d www.hugo.wang -a manual certonly

Reload Nginx & That's it.

::

    $ sudo nginx -s reload

---------

See this article for how to config SSL certs in Nginx:
https://mitnk.com/2015/11/lets_encrypt/

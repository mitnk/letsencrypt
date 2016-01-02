Let's Encrypt Client
====================

Disclaimer
----------

This is a **Hack Version** based on the Let's Encrypt offical Client.
It's targeted on Nginx.


Preparation
-----------

The **Preparation** only need to do once. Jump to **Usage** section
if you already done it.

1) **Checkout the code & build the virtualenv**

```
$ git clone https://github.com/mitnk/letsencrypt
$ cd letsencrypt
$ sudo ./letsencrypt-auto --help
```

This will create an virtualenv at `~/.local/share/letsencrypt`.

2) **Setup Nginx Configs**

Put the following code into every domain server config
in you Nginx.

*Note: Please change `mitnk` to your username*

```
location /.well-known/acme-challenge/ {
    default_type text/plain;
    alias /home/mitnk/.well-known/acme-challenge/;
}
```

Create the directories:

```
$ mkdir -p /home/mitnk/.well-known/acme-challenge/
```

Then reload Nginx (e.g. `sudo nginx -s reload`).


Usage
-----

```
```

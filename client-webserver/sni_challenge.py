#!/usr/bin/env python

import subprocess
import M2Crypto
from Crypto import Random
import hmac
import hashlib
from shutil import move
from os import remove, close
import binascii
import augeas
import configurator
#import dns.resolver

#CHOC_DIR = "/home/ubuntu/chocolate/client-webserver/"
CHOC_DIR = "/home/james/Documents/apache_choc/"
CHOC_CERT_CONF = "choc_cert_extensions.cnf"
OPTIONS_SSL_CONF = CHOC_DIR + "options-ssl.conf"
APACHE_CHALLENGE_CONF = CHOC_DIR + "choc_sni_cert_challenge.conf"
S_SIZE = 32
NONCE_SIZE = 32

def getChocCertFile(nonce):
    """
    Returns standardized name for challenge certificate

    nonce:  string - hex
    
    result: returns certificate file name
    """

    return CHOC_DIR + nonce + ".crt"

def findApacheConfigFile():
    """
    Locates the file path to the user's main apache config

    TODO: This needs to be rewritten... should use true ServerRoot
    
    result: returns file path if present
    """
    # This needs to be fixed to account for multiple httpd.conf files
    try:
        p = subprocess.check_output(["sudo", "find", "/etc", "-name", "httpd.conf"], stderr=open("/dev/null"))
	p = p[:len(p)-1]
	print "Apache Config: ", p
	return p
    except subprocess.CalledProcessError, e:
        print "httpd.conf not found"
	print "Please include .... in the conf file"
        return None

def getConfigText(nonce, ip_addrs, key):
    """
    Chocolate virtual server configuration text

    nonce:      string - hex
    ip_addr:    string - address of challenged domain
    key:        string - file path to key

    result:     returns virtual host configuration text
    """
    configText = "<VirtualHost " + " ".join(ip_addrs) + "> \n \
ServerName " + nonce + ".chocolate \n \
UseCanonicalName on \n \
SSLStrictSNIVHostCheck on \n \
\n \
LimitRequestBody 1048576 \n \
\n \
Include " + OPTIONS_SSL_CONF + " \n \
SSLCertificateFile " + getChocCertFile(nonce) + " \n \
SSLCertificateKeyFile " + key + " \n \
\n \
DocumentRoot " + CHOC_DIR + "challenge_page/ \n \
</VirtualHost> \n\n "

    return configText

def modifyApacheConfig(mainConfig, nonce, listlistAddrs, key, configurator):
    """
    Modifies Apache config files to include the challenge virtual servers
    
    mainConfig:    string - file path to Apache user config file
    listSNITuple:  list of tuples with form (addr, y, nonce, ext_oid)
                   addr (string), y (byte array), nonce (hex string), ext_oid (string)
    key:           string - file path to key

    result:        Apache config includes virtual servers for issued challenges
    """

    # TODO: Use ip address of existing vhost instead of relying on FQDN
    configText = "<IfModule mod_ssl.c> \n"
    for lis in listlistAddrs:
        configText += getConfigText(nonce, lis, key)
    configText += "</IfModule> \n"

    checkForApacheConfInclude(mainConfig, configurator)
    newConf = open(APACHE_CHALLENGE_CONF, 'w')
    newConf.write(configText)
    newConf.close()

# Need to add NameVirtualHost IP_ADDR or does the chocolate install do this?
def checkForApacheConfInclude(mainConfig, configurator):
    """
    Adds chocolate challenge include file if it does not already exist 
    within mainConfig
    
    mainConfig:  string - file path to main user apache config file

    result:      User Apache configuration includes chocolate sni challenge file
    """
    if len(configurator.find_directive("Include", APACHE_CHALLENGE_CONF)) == 0:
        print "Including challenge virtual host(s)"
        configurator.add_dir("/files" + mainConfig, "Include", APACHE_CHALLENGE_CONF)

def createChallengeCert(oid, ext, nonce, csr, key):
    """
    Modifies challenge certificate configuration and calls openssl binary to create a certificate

    oid:    string
    ext:    string - hex z value
    nonce:  string - hex
    csr:    string - file path to csr
    key:    string - file path to key

    result: certificate created at getChocCertFile(nonce)
    """

    updateCertConf(oid, ext)
    subprocess.call(["openssl", "x509", "-req", "-days", "21", "-extfile", CHOC_CERT_CONF, "-extensions", "v3_ca", "-signkey", key, "-out", getChocCertFile(nonce), "-in", csr])
    

def generateExtension(key, y):
    """
    Generates z to be placed in certificate extension

    key:    string - File path to key
    y:      byte array

    result: returns z value
    """

    rsaPrivKey = M2Crypto.RSA.load_key(key)
    r = rsaPrivKey.private_decrypt(y, M2Crypto.RSA.pkcs1_oaep_padding)

    s = Random.get_random_bytes(S_SIZE)
    extHMAC = hmac.new(r, str(s), hashlib.sha256)
    return byteToHex(s) + extHMAC.hexdigest()

def byteToHex(byteStr):
    """
    Converts binary array to hex string
    
    byteStr:  byte array
    
    result: returns hex representation of byteStr
    """

    return ''.join(["%02X" % ord(x) for x in byteStr]).strip()

#Searches for the first extension specified in binary
def updateCertConf(oid, value):
    """
    Updates the sni_challenge openssl certificate config file

    oid:    string - ex. 1.3.3.7 
    value   string hex - value of OID

    result: updated certificate config file
    """

    confOld = open(CHOC_CERT_CONF)
    confNew = open(CHOC_CERT_CONF + ".tmp", 'w')
    flag = False
    for line in confOld:
        if "=critical, DER:" in line:
            confNew.write(oid + "=critical, DER:" + value + "\n")
            flag = True
        else:
            confNew.write(line)
    if flag is False:
        print "Error: Could not find extension in CHOC_CERT_CONF"
        exit()
    confNew.close()
    confOld.close()
    remove(CHOC_CERT_CONF)
    move(CHOC_CERT_CONF + ".tmp", CHOC_CERT_CONF)

def apache_restart():
    """
    Restarts apache server
    """
    subprocess.call(["sudo", "/etc/init.d/apache2", "reload"])

def cleanup(listSNITuple, configurator):
    """
    Remove all temporary changes necessary to perform the challenge
    
    configurator:  Configurator object
    listSNITuple:  The initial challenge tuple

    result: Apache server is restored to the pre-challenge state
    """
    configurator.revert_config()
    apache_restart()
    remove_files(listSNITuple)
    

def remove_files(listSNITuple):
    """
    Removes all of the temporary SNI files
    """
    for tup in listSNITuple:
        remove(getChocCertFile(tup[2]))
    remove(APACHE_CHALLENGE_CONF)

#main call
def perform_sni_cert_challenge(listSNITuple, csr, key, configurator):
    """
    Sets up and reloads Apache server to handle SNI challenges

    listSNITuple:  List of tuples with form (addr, y, nonce, ext_oid)
                   addr (string), y (byte array), nonce (hex string), 
                   ext_oid (string)
    csr:           string - File path to chocolate csr
    key:           string - File path to key
    configurator:  Configurator obj
    """
    # Save any changes to the configuration as a precaution
    # About to make temporary changes to the config
    configurator.save("Before performing sni_challenge")

    addresses = []
    default_addr = "*:443"
    for tup in listSNITuple:
        vhost = configurator.choose_virtual_host(tup[0])
        if vhost is None:
            print "No vhost exists with servername or alias of:", tup[0]
            print "No _default_:443 vhost exists"
            print "Please specify servernames in the Apache config"
            return False
            
        if not configurator.make_server_sni_ready(vhost, default_addr):
            return False

        for a in vhost.addrs:
            if "_default_" in a:
                addresses.append([default_addr])
                break
        else:
            addresses.append(vhost.addrs)

    for tup in listSNITuple:
        ext = generateExtension(key, tup[1])
        createChallengeCert(tup[3], ext, tup[2], csr, key)
    
    modifyApacheConfig(findApacheConfigFile(), tup[2], addresses, key, configurator)
    # Save reversible changes and restart the server
    configurator.save("SNI Challenge", True)
    apache_restart()

def main():
    key = CHOC_DIR + "key.pem"
    csr = CHOC_DIR + "req.pem"

    testkey = M2Crypto.RSA.load_key(key)
    
    r = Random.get_random_bytes(S_SIZE)
    r = "testValueForR"
    nonce = Random.get_random_bytes(NONCE_SIZE)
    nonce = "nonce"
    r2 = "testValueForR2"
    nonce2 = "nonce2"
    
    #ans = dns.resolver.query("google.com")
    #print ans.rrset
    #return
    #the second parameter is ignored
    #https://www.dlitz.net/software/pycrypto/api/current/
    y = testkey.public_encrypt(r, M2Crypto.RSA.pkcs1_oaep_padding)
    y2 = testkey.public_encrypt(r2, M2Crypto.RSA.pkcs1_oaep_padding)

    nonce = binascii.hexlify(nonce)
    nonce2 = binascii.hexlify(nonce2)
    
    config = configurator.Configurator()

    challenges = [("example.com", y, nonce, "1.3.3.7"), ("www.example.com",y2, nonce2, "1.3.3.7")]
    #challenges = [("127.0.0.1", y, nonce, "1.3.3.7"), ("localhost", y2, nonce2, "1.3.3.7")]
    perform_sni_cert_challenge(challenges, csr, key, config)

    # Waste some time without importing time module... just for testing
    for i in range(0, 12000):
        if i % 2000 == 0:
            print "Waiting:", i

    print "Cleaning up"
    cleanup(challenges, config)

if __name__ == "__main__":
    main()

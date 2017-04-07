nginx:
  pkg:
    - name: nginx-full
    - latest
  service.running:
    - enable: True

/etc/nginx/sites-enabled/my_vhost.conf:
  file.managed:
    - source: salt://files/my_vhost_conf.jinja
    - template: jinja
    - require:
      - pkg: nginx
    - user: root
    - group: root
    - mode: 644
    - context:
        {{ salt['grains.get']('my_grains') }}
    - watch_in:
      - service: nginx

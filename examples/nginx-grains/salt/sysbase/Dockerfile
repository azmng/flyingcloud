FROM centos:centos6

CMD ["/bin/bash"]

# Add EPEL repository
RUN yum -y install epel-release 

# Install salt and deps
RUN yum -y install salt-minion python2-gnupg cronie

# SaltStack fail hard if any state fails
RUN echo "failhard: True" >> /etc/salt/minion

# Clean up YUM when done.
RUN yum clean all && rm -rf /tmp/* /var/tmp/*

# 1) Grab the display number from $DISPLAY (10) and the cookie from your entry
dnum=$(printf '%s\n' "$DISPLAY" | sed -E 's/.*:([0-9]+).*/\1/')
cookie=$(xauth list | awk -v dnum="$dnum" '$0 ~ "/unix:" dnum" " {print $NF; exit}')

# 2) Sanity check
echo "Display #: $dnum"
echo "Cookie: $cookie"

# 3) Add equivalent entries into *root*'s authority for both names
sudo XAUTHORITY=/root/.Xauthority xauth add "localhost:$dnum" MIT-MAGIC-COOKIE-1 "$cookie"
sudo XAUTHORITY=/root/.Xauthority xauth add "$(hostname)/unix:$dnum" MIT-MAGIC-COOKIE-1 "$cookie"

# (Optional) If you also want your *user* .Xauthority to have the localhost alias:
xauth add "localhost:$dnum" MIT-MAGIC-COOKIE-1 "$cookie"

@echo "Remember to launch the ssh with command: ssh -Y -o ForwardX11Trusted=yes -o ForwardX11Timeout=0 yxm319@pc771.emulab.net"